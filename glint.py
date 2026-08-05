#!/usr/bin/env python3
"""glint — a rich, fast, fail-safe status line for Claude Code.

Reads the Status hook JSON on stdin and prints one ANSI-colored line:

  ✻ F5  ┃  📁 my-repo   🌿 main ●3 ↑1  ┃  💰 $0.42 · 4m   +1.2k/-340  ┃  🧠 36% 357k/1.0M   ♻️ 94%   ⏱5h 63% ↻1h07m 📅7d 10% ↻2.3d  ┃  ☕ 52m

Segments are grouped by topic — session ┃ place ┃ change ┃ budget ┃ rest — with a
dim bar between groups. Percentages carry their own colour; set `GLINT_BARS=1`
for `▕███░░▏` gauges next to them. The last group is a break reminder: it stays
hidden until you've worked 30 unbroken minutes (`GLINT_REST=0` turns it off).
Took the break? `python3 glint.py --rested` zeroes the clock; `--rest-status`
prints it. Ten minutes with no render resets it on its own.

Every segment degrades gracefully: a missing field means the segment is omitted,
never an error. A crash falls back to a minimal badge so the bar never goes blank.

Repo: https://github.com/oleg-koval/glint
"""

from __future__ import annotations

__version__ = "1.1.0"   # keep in step with CHANGELOG.md and the git tag

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata

# ── 256-color palette ─────────────────────────────────────────────────────────
CORAL = 209      # model badge (Anthropic-ish)
BLUE = 75        # directory
GREEN = 114      # git clean / cheap / added
YELLOW = 179     # git dirty / mid cost
RED = 174        # behind / expensive / removed
GOLD = 220       # cost
DIM = 244        # separators, labels
PURPLE = 141     # worktree
RULE = 242       # topic divider — visible as structure, dimmer than any data

# ── Segment priority: dropped first when the window is narrow ──────────────
PRIO_MODEL = 0       # never dropped
PRIO_CONTEXT = 1
PRIO_DIR = 2
PRIO_GIT = 3
PRIO_WORKTREE = 4
PRIO_RATELIMIT = 5
PRIO_CACHE = 6
PRIO_LINES = 7
PRIO_COST = 8        # dropped first
PRIO_PR = 3.5        # open PR for this branch: keep it near git, drop before worktree
# The break nudge earns its place as it escalates: idle chatter while it's just
# a clock, hard to drop once it's telling you to stand up.
PRIO_REST_QUIET = 6.5
PRIO_REST_NUDGE = 3.2
PRIO_REST_HARD = 1.2

# ── Topic groups: related segments sit together, divided by a faint rule ───────
# Reading order is "who am I → where am I → what changed → what's it costing →
# how long have I been sitting here".
GRP_SESSION = 0      # model, effort, fast mode
GRP_PLACE = 1        # directory, worktree, branch, PR
GRP_CHANGE = 2       # cost, lines added/removed
GRP_BUDGET = 3       # context, cache hits, rate-limit windows
GRP_REST = 4         # how long you've been at it without a break


def c(text: str, color: int, *, bold: bool = False) -> str:
    b = "1;" if bold else ""
    return f"\033[{b}38;5;{color}m{text}\033[0m"


def sep() -> str:
    """Gap between two segments of the same topic."""
    return c("  ", DIM)


def group_sep() -> str:
    """Divider between topics: a bar with a wider gap than the one inside a
    group, so the boundary is unmistakable while staying dimmer than any data."""
    return c("  ┃  ", RULE)


def enabled(name: str) -> bool:
    """Is segment `name` switched on? `GLINT_COST=0` hides the cost segment.

    Off is any of 0/false/no/off; anything else (including unset) is on, so the
    default stays "show everything" and a typo can't silently blank a segment.
    """
    v = os.environ.get(f"GLINT_{name}")
    return True if v is None else v.strip().lower() not in ("0", "false", "no", "off")


def opt_in(name: str) -> bool:
    """Is opt-in feature `name` switched on? Unset means off.

    The mirror of `enabled()`: used for things that stay out of the way unless
    asked for, like the `▕███░░▏` gauges (`GLINT_BARS=1`).
    """
    v = os.environ.get(f"GLINT_{name}")
    return bool(v) and v.strip().lower() in ("1", "true", "yes", "on")


_ANSI = re.compile(r"\033\[[0-9;]*m")
# OSC 8 hyperlinks: \033]8;;URL\033\\ ... \033]8;;\033\\ — invisible, so strip before measuring.
_OSC8 = re.compile(r"\033\]8;;[^\033\a]*(?:\033\\|\a)")


def vis_width(s: str) -> int:
    """Rendered cell width: ANSI is invisible, emoji take two cells."""
    w = 0
    for ch in _OSC8.sub("", _ANSI.sub("", s)):
        o = ord(ch)
        if o == 0xFE0F or unicodedata.combining(ch):
            continue                      # variation selector / combining mark
        if (0x1F300 <= o <= 0x1FAFF) or (0x2600 <= o <= 0x27BF) or (0x2B00 <= o <= 0x2BFF):
            w += 2                        # pictographs render double-wide
        else:
            w += 1
    return w


def term_width(default: int = 120) -> int:
    """Columns available. stdout is a pipe here, so ask the tty directly."""
    for fd in (2, 1):
        try:
            cols = os.get_terminal_size(fd).columns
            if cols > 0:
                return cols
        except Exception:
            pass
    try:
        with open("/dev/tty") as tty:
            cols = os.get_terminal_size(tty.fileno()).columns
            if cols > 0:
                return cols
    except Exception:
        pass
    try:
        cols = int(os.environ.get("COLUMNS") or 0)
        if cols > 0:
            return cols
    except Exception:
        pass
    return default


def fit(segments: list[tuple[int, int, str]], width: int) -> str:
    """Join segments by topic, dropping the least important until the line fits.

    Each segment is (priority, group, text); HIGHER priority is dropped first,
    and priority 0 is never dropped — a narrow window loses detail, not identity.
    Segments of one group are separated by a gap, groups by a faint rule. The
    rule is drawn between surviving groups only, so dropping a whole topic never
    leaves a dangling divider.
    """
    def render(items) -> str:
        out, prev_group = "", None
        for _, (_, group, text) in sorted(items):
            if prev_group is not None:
                out += sep() if group == prev_group else group_sep()
            out += text
            prev_group = group
        return out

    keep = list(enumerate(segments))       # carry original index to keep order
    dropped = []
    while len(keep) > 1:
        if vis_width(render(keep)) <= width:
            break
        worst = max(range(len(keep)), key=lambda i: keep[i][1][0])
        if keep[worst][1][0] == 0:
            break                          # only essentials left; let it clip
        dropped.append(keep.pop(worst))

    # Greedy removal can drop a small segment and then a large one that alone
    # would have sufficed, wasting space. Put back whatever still fits, most
    # important first.
    for item in sorted(dropped, key=lambda it: it[1][0]):
        trial = keep + [item]
        if vis_width(render(trial)) <= width:
            keep = trial

    return render(keep)


def link(text: str, url: str) -> str:
    """Wrap text in an OSC 8 hyperlink so the terminal makes it clickable.

    Terminals that do not support OSC 8 ignore the escapes and show the text, so this is safe
    everywhere. `vis_width` strips the sequences, otherwise `fit` would count the URL.
    """
    if not url:
        return text
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


_PR_TTL = 180.0        # seconds a cached lookup stays fresh
_PR_NEGATIVE_TTL = 60.0  # shorter, so a PR opened moments ago shows up quickly


def _pr_cache_path(repo_root: str, branch: str) -> str:
    key = hashlib.sha1(f"{repo_root}\0{branch}".encode()).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), f"glint-pr-{os.getuid()}-{key}.json")


def _pr_refresh(cache: str, dir_: str, branch: str) -> None:
    """Fetch the PR for `branch` and write it to `cache`. Runs detached; never blocks a render."""
    try:
        out = subprocess.run(
            [
                "gh", "pr", "list", "--head", branch, "--state", "open", "--limit", "1",
                "--json", "number,url,isDraft,statusCheckRollup",
            ],
            capture_output=True, text=True, timeout=15.0, cwd=dir_,
        )
        rows = json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else []
    except Exception:
        rows = []

    payload = {"at": time.time(), "pr": rows[0] if rows else None}
    try:
        tmp = f"{cache}.{os.getpid()}"
        with open(tmp, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, cache)             # atomic: a reader never sees a half-written file
    except Exception:
        pass


def pr_for_branch(dir_: str, branch: str) -> dict | None:
    """Cached open-PR lookup for `branch`.

    `gh` is a network call and the status line renders constantly, so this never waits for it.
    A stale entry is shown immediately and refreshed in the background; a cold cache shows
    nothing this render and appears on the next one.
    """
    if not branch or branch == "HEAD":
        return None

    root = git(dir_, "rev-parse", "--show-toplevel") or dir_
    cache = _pr_cache_path(root, branch)

    payload, age = None, None
    try:
        with open(cache) as fh:
            payload = json.load(fh)
        age = time.time() - float(payload.get("at") or 0)
    except Exception:
        payload = None

    ttl = _PR_TTL if (payload or {}).get("pr") else _PR_NEGATIVE_TTL
    if payload is None or age is None or age > ttl:
        if os.environ.get("GLINT_PR_SYNC"):          # tests want a deterministic result
            _pr_refresh(cache, dir_, branch)
            try:
                with open(cache) as fh:
                    payload = json.load(fh)
            except Exception:
                return None
        else:
            try:
                subprocess.Popen(
                    [sys.executable, os.path.abspath(__file__), "--refresh-pr", cache, dir_, branch],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                )
            except Exception:
                pass

    return (payload or {}).get("pr")


def pr_segment(pr: dict) -> str:
    """`⇄ #179 ✓` — number is a clickable link, glyph is the CI rollup."""
    number = pr.get("number")
    if not number:
        return ""

    states = {
        (n.get("conclusion") or n.get("status") or "").upper()
        for n in (pr.get("statusCheckRollup") or [])
        if isinstance(n, dict)
    }
    if {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"} & states:
        mark, color = "✗", RED
    elif {"IN_PROGRESS", "PENDING", "QUEUED", "WAITING", "REQUESTED"} & states:
        mark, color = "•", YELLOW
    elif {"SUCCESS", "NEUTRAL", "SKIPPED"} & states:
        mark, color = "✓", GREEN
    else:
        mark, color = "", DIM

    label = f"#{number}"
    if pr.get("isDraft"):
        label += " draft"
    out = "⇄ " + c(link(label, pr.get("url") or ""), DIM if pr.get("isDraft") else BLUE)
    return f"{out} {c(mark, color)}" if mark else out


def git(dir_: str, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", dir_, *args],
            capture_output=True, text=True, timeout=1.0,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def human_dur(ms: float) -> str:
    s = int(ms / 1000)
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h = m // 60
    return f"{h}h{m % 60:02d}m"


def human_lines(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


# family name → single-letter badge prefix (S5, O4.8, H4.5, F5, ...)
_FAMILY_INITIALS = {"sonnet": "S", "opus": "O", "haiku": "H", "fable": "F"}


def model_short(name: str) -> str:
    # "Sonnet 4.6" / "claude-opus-4-8" → "S4.6" / "O4.8"; unknown families kept as-is
    n = name.replace("Claude ", "").strip()
    low = n.lower()
    for fam, initial in _FAMILY_INITIALS.items():
        if fam in low:
            m = re.search(r"(\d+(?:[.\-]\d+)?)", n[low.index(fam) + len(fam):])
            return f"{initial}{m.group(1).replace('-', '.')}" if m else initial
    return n or name


# reasoning effort → (badge, color), ordered low→max as Claude Code exposes it.
# Colored by what it costs you, so the badge earns its space rather than just
# restating a setting.
_EFFORT = {
    "low": ("L", GREEN),
    "medium": ("M", GREEN),
    "high": ("H", GOLD),
    "xhigh": ("X", RED),
    "max": ("MAX", RED),
}


def tok_h(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n // 1000}k"
    return str(n)


def context_usage(transcript_path: str) -> tuple[int, int]:
    """(total input-side tokens, cache-read tokens) of the last assistant turn.

    Reads the transcript JSONL backwards and returns the first usage block found
    on the main thread. (0, 0) if unavailable.
    """
    try:
        with open(transcript_path, "rb") as f:
            lines = f.readlines()
    except Exception:
        return 0, 0
    for line in reversed(lines):
        try:
            o = json.loads(line)
        except Exception:
            continue
        # Only the main thread's assistant turns reflect real context — skip
        # sub-agent (sidechain) usage so the gauge never shows a delegate's size.
        if o.get("type") != "assistant" or o.get("isSidechain"):
            continue
        u = (o.get("message") or {}).get("usage") or o.get("usage")
        if u:
            cached = int(u.get("cache_read_input_tokens", 0))
            total = (
                int(u.get("input_tokens", 0))
                + int(u.get("cache_creation_input_tokens", 0))
                + cached
            )
            return total, cached
    return 0, 0


def seconds_until(value):
    """Seconds until an epoch-seconds or ISO-8601 instant. None if unparseable/past."""
    import datetime as dt
    try:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            target = dt.datetime.fromtimestamp(value, dt.timezone.utc)
        elif isinstance(value, str):
            target = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if target.tzinfo is None:
                target = target.replace(tzinfo=dt.timezone.utc)
        else:
            return None
        left = (target - dt.datetime.now(dt.timezone.utc)).total_seconds()
        return left if left > 0 else None
    except Exception:
        return None


def reset_eta(value) -> str:
    """Human ETA until a rate-limit reset. Accepts epoch seconds or ISO-8601."""
    left = seconds_until(value)
    if left is None:
        return ""
    if left < 3600:
        return f"{int(left // 60)}m"
    if left < 86400:
        return f"{int(left // 3600)}h{int(left % 3600 // 60):02d}m"
    return f"{left / 86400:.1f}d"


def pace(used_pct: float, secs_left, window_secs: int):
    """Projected share of quota needed by reset if the current burn rate holds.

    1.4 means "at this rate you'd need 140% of the window's quota" — i.e. you run
    out before it resets. None when the window is too young to extrapolate from.
    """
    if secs_left is None:
        return None
    elapsed = 1 - min(max(secs_left / window_secs, 0.0), 1.0)
    if elapsed < 0.05:  # too early — a few tokens would project to absurd numbers
        return None
    return (used_pct / 100) / elapsed


def gauge(pct: float, width: int = 8) -> str:
    filled = round(pct * width)
    return "▕" + "█" * filled + "░" * (width - filled) + "▏"


# ── Rest reminder ─────────────────────────────────────────────────────────────
# Thresholds, in minutes of unbroken work. The defaults follow the ergonomics
# consensus rather than a single method: sitting research says break up long
# stretches well before the hour; DeskTime's productivity study put the most
# effective rhythm near 52 minutes on / 17 off; and attention runs on ~90-minute
# ultradian cycles, past which you're paying fatigue to stay in the chair.
# Nothing appears before REST_SHOW, so a short session never sees this segment.
REST_SHOW = 30.0     # start showing the clock, quietly
REST_NUDGE = 50.0    # you've earned a break
REST_HARD = 90.0     # one ultradian cycle — actually get up
REST_GAP = 10.0      # no render for this long means you were away: clock resets


def env_minutes(name: str, default: float) -> float:
    """A minutes threshold from `GLINT_<name>`, ignoring junk and non-positives."""
    try:
        v = float(os.environ.get(f"GLINT_{name}", ""))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _rest_state_path() -> str:
    # One file per user, not per session: it's one body across every window, so
    # two Claude Code windows share the same work clock. GLINT_REST_STATE moves
    # it (tests point it at a throwaway file rather than your real clock).
    return os.environ.get("GLINT_REST_STATE") or os.path.join(
        tempfile.gettempdir(), f"glint-rest-{os.getuid()}.json"
    )


def rest_minutes(now: float | None = None, path: str | None = None):
    """Minutes worked without a break, tracking state across renders.

    Claude Code renders on activity, so the gap between two renders is idle time.
    A gap of `GLINT_REST_GAP` minutes or more counts as a real break and restarts
    the clock — which is also the feedback that the break landed, since the
    segment disappears. Returns None if the state file can't be used; a status
    line is never worth an error.
    """
    now = time.time() if now is None else now
    path = path or _rest_state_path()
    gap = env_minutes("REST_GAP", REST_GAP) * 60

    start = now
    try:
        with open(path) as f:
            prev = json.load(f)
        last, prev_start = float(prev["last"]), float(prev["start"])
        # Clock skew or a stale file from a past boot: treat as a fresh start.
        if 0 <= now - last < gap and prev_start <= now:
            start = prev_start
    except Exception:
        pass

    try:
        tmp = f"{path}.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump({"start": start, "last": now}, f)
        os.replace(tmp, path)
    except Exception:
        return None

    return (now - start) / 60


def rest_segment(mins: float) -> str:
    """The break nudge, or "" while you're still inside a healthy stretch.

    Three rungs, each saying what it wants in words — an emoji and a number
    alone would leave you guessing which clock this even is:

        🪑 34m            just tracking; nothing to do
        ☕ 52m break      you've earned one
        🛑 1h35m stand up you're past a full cycle
    """
    nudge = env_minutes("REST_NUDGE", REST_NUDGE)
    show = env_minutes("REST_SHOW", min(REST_SHOW, nudge * 0.6))
    hard = env_minutes("REST_HARD", nudge * 1.8)
    if mins < show:
        return ""
    clock = f"{int(mins)}m" if mins < 60 else f"{int(mins // 60)}h{int(mins % 60):02d}m"
    if mins >= hard:
        return c(f"🛑 {clock} stand up", RED, bold=True)
    if mins >= nudge:
        return "☕ " + c(clock, YELLOW, bold=True) + c(" break", YELLOW)
    return c(f"🪑 {clock}", DIM)


def rest_reset(path: str | None = None) -> bool:
    """Start the work clock over, as if you'd just sat down. True if it stuck.

    This is the "yes, I actually took that break" button: a stretch break shorter
    than `GLINT_REST_GAP` wouldn't reset the clock on its own, and nobody wants a
    reminder that keeps shouting at someone who already listened.
    """
    now = time.time()
    path = path or _rest_state_path()
    try:
        tmp = f"{path}.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump({"start": now, "last": now}, f)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def rest_priority(mins: float) -> float:
    """How hard the nudge fights to stay on a narrow line: harder the later it is."""
    nudge = env_minutes("REST_NUDGE", REST_NUDGE)
    if mins >= env_minutes("REST_HARD", nudge * 1.8):
        return PRIO_REST_HARD
    if mins >= nudge:
        return PRIO_REST_NUDGE
    return PRIO_REST_QUIET


def bar(pct: float, color: int, width: int = 8) -> str:
    """Gauge as a leading fragment, or "" unless `GLINT_BARS=1`.

    Off by default: the coloured percentage already carries the signal, and the
    blocks cost ~10 cells that a narrow window would rather spend on a segment.
    """
    return " " + c(gauge(pct, width), color) if opt_in("BARS") else ""


def main() -> None:
    # Detached background refresh spawned by pr_for_branch; not a status-line render.
    if len(sys.argv) == 5 and sys.argv[1] == "--refresh-pr":
        _pr_refresh(sys.argv[2], sys.argv[3], sys.argv[4])
        return

    # Claude Code always calls this with JSON on stdin and no arguments, so these
    # flags are free for humans. `--rested` is how you tell it the break happened.
    if len(sys.argv) == 2 and sys.argv[1] in ("--rested", "--rest-reset"):
        ok = rest_reset()
        print("☕ break logged — work clock back to zero" if ok
              else "couldn't write the rest clock; nothing changed")
        sys.exit(0 if ok else 1)
    if len(sys.argv) == 2 and sys.argv[1] in ("--version", "-V"):
        print(f"glint {__version__}")
        return
    if len(sys.argv) == 2 and sys.argv[1] == "--rest-status":
        mins = rest_minutes()
        if mins is None:
            print("rest clock unavailable")
            sys.exit(1)
        print(f"{int(mins)} min since your last break "
              f"(nudge at {int(env_minutes('REST_NUDGE', REST_NUDGE))})")
        return

    raw = sys.stdin.read()
    try:
        d = json.loads(raw)
    except Exception:
        d = {}

    segments: list[tuple[int, int, str]] = []   # (priority, group, text); see fit()

    # ── Model badge, with reasoning effort and fast mode ──
    model = (d.get("model") or {}).get("display_name") or (d.get("model") or {}).get("id") or "Claude"
    badge = c("✻ ", CORAL, bold=True) + c(model_short(model), CORAL, bold=True)
    # Only sent for models that actually support effort — absent is not "high".
    level = str((d.get("effort") or {}).get("level") or "").lower()
    if level in _EFFORT:
        mark, ec = _EFFORT[level]
        badge += " " + c(mark, ec, bold=level in ("xhigh", "max"))
    if d.get("fast_mode"):
        badge += " ⏩"
    segments.append((PRIO_MODEL, GRP_SESSION, badge))

    # ── Directory ──
    cwd = (d.get("workspace") or {}).get("current_dir") or d.get("cwd") or os.getcwd()
    home = os.path.expanduser("~")
    label = "~" if cwd == home else os.path.basename(cwd.rstrip("/")) or cwd
    segments.append((PRIO_DIR, GRP_PLACE, "📁 " + c(label, BLUE)))

    # ── Worktree ── (only when it adds information the directory segment lacks)
    wt = (d.get("worktree") or {}).get("name") or (d.get("workspace") or {}).get("git_worktree")
    if not wt:
        # Older Claude Code has no such field: a linked worktree is one whose
        # private git dir differs from the repo's common dir.
        gd, common = git(cwd, "rev-parse", "--absolute-git-dir"), git(cwd, "rev-parse", "--git-common-dir")
        if gd and common and os.path.abspath(gd) != os.path.abspath(common):
            wt = os.path.basename(git(cwd, "rev-parse", "--show-toplevel") or "")
    wt = os.path.basename(str(wt).rstrip("/")) if wt else ""
    if enabled("WORKTREE") and wt and wt != label:
        segments.append((PRIO_WORKTREE, GRP_PLACE, "🌳 " + c(wt, PURPLE)))

    # ── Git ──
    branch = git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if branch:
        porcelain = git(cwd, "status", "--porcelain")
        dirty = len([ln for ln in porcelain.splitlines() if ln.strip()])
        color = YELLOW if dirty else GREEN
        g = "🌿 " + c(branch, color)
        if dirty:
            g += c(f" ●{dirty}", YELLOW)
        # ahead / behind upstream
        ab = git(cwd, "rev-list", "--count", "--left-right", "@{u}...HEAD")
        if ab and "\t" in ab:
            behind, ahead = ab.split("\t")
            if ahead != "0":
                g += c(f" ↑{ahead}", GREEN)
            if behind != "0":
                g += c(f" ↓{behind}", RED)
        segments.append((PRIO_GIT, GRP_PLACE, g))

        # ── Open pull request for this branch ── (cached; never blocks a render)
        if enabled("PR"):
            pr = pr_for_branch(cwd, branch)
            if pr:
                seg = pr_segment(pr)
                if seg:
                    segments.append((PRIO_PR, GRP_PLACE, seg))

    # ── Cost · duration ──
    cost = d.get("cost") or {}
    money = cost.get("total_cost_usd")
    if enabled("COST") and isinstance(money, (int, float)) and money > 0:
        money_color = GREEN if money < 1 else GOLD if money < 5 else RED
        bits = ["💰 " + c(f"${money:.2f}", money_color)]
        dur = cost.get("total_duration_ms")
        if isinstance(dur, (int, float)) and dur > 0:
            bits.append(c(human_dur(dur), DIM))
        segments.append((PRIO_COST, GRP_CHANGE, c(" · ", DIM).join(bits)))

    # ── Lines changed ──
    added = cost.get("total_lines_added")
    removed = cost.get("total_lines_removed")
    if enabled("LINES") and (added or removed):
        la = c(f"+{human_lines(added or 0)}", GREEN)
        lr = c(f"-{human_lines(removed or 0)}", RED)
        segments.append((PRIO_LINES, GRP_CHANGE, f"{la}{c('/', DIM)}{lr}"))

    # ── Live context gauge ──
    tx_tokens, tx_cached = context_usage(d.get("transcript_path") or "")
    cw = d.get("context_window") or {}
    used_pct = cw.get("used_percentage")
    limit = cw.get("context_window_size")
    tokens = cw.get("total_input_tokens") or tx_tokens
    # The reported size already accounts for a 1M window, so trust it whenever
    # it's there — used_percentage is null until the first turn, and guessing the
    # limit then would draw a 1M session against 200k.
    if not isinstance(limit, (int, float)) or limit <= 0:
        limit = 1_000_000 if (tokens > 200_000 or d.get("exceeds_200k_tokens")) else 200_000
    if isinstance(used_pct, (int, float)):
        pct = min(max(used_pct / 100, 0.0), 1.0)
    else:
        pct = min(tokens / limit, 1.0)

    if tokens > 0:
        gc = GREEN if pct < 0.6 else YELLOW if pct < 0.85 else RED
        seg = (
            "🧠 " + c(f"{pct * 100:.0f}%", gc, bold=True)
            + bar(pct, gc)
            + " " + c(f"{tok_h(tokens)}/{tok_h(limit)}", DIM)
        )
        # Approaching auto-compact: show how much runway is actually left.
        headroom = int(limit * (1 - pct))
        if pct >= 0.85:
            seg += "  " + c(f"⚠ {tok_h(headroom)} left", RED, bold=True)
        elif pct >= 0.7:
            seg += "  " + c(f"→ {tok_h(headroom)} left", YELLOW)
        segments.append((PRIO_CONTEXT, GRP_BUDGET, seg))

    # ── Prompt-cache efficiency (cache reads are ~10x cheaper than fresh input) ──
    if enabled("CACHE") and tx_tokens > 0:
        ratio = tx_cached / tx_tokens
        rc = GREEN if ratio >= 0.8 else YELLOW if ratio >= 0.5 else RED
        segments.append((PRIO_CACHE, GRP_BUDGET, "♻️ " + c(f"{ratio * 100:.0f}%", rc)))

    # ── Rate limit windows (5h session / 7d weekly) ──
    rl = d.get("rate_limits") or {} if enabled("RATELIMITS") else {}
    rl_bits = []
    for key, icon, name, window in (
        ("five_hour", "⏱", "5h", 5 * 3600),
        ("seven_day", "📅", "7d", 7 * 86400),
    ):
        entry = rl.get(key) or {}
        used = entry.get("used_percentage")
        if isinstance(used, (int, float)):
            rpct = min(max(used / 100, 0.0), 1.0)
            rc = GREEN if rpct < 0.6 else YELLOW if rpct < 0.85 else RED
            bit = f"{icon}{name} " + c(f"{used:.0f}%", rc, bold=True) + bar(rpct, rc, width=5)
            secs_left = seconds_until(entry.get("resets_at"))
            eta = reset_eta(entry.get("resets_at"))
            if eta:
                bit += c(f" ↻{eta}", DIM)
            # Burning faster than the window elapses → you'd hit the cap early.
            p = pace(used, secs_left, window)
            if p is not None and p > 1.05:
                bit += " " + c(f"⚡{p * 100:.0f}%", RED if p > 1.5 else YELLOW, bold=p > 1.5)
            rl_bits.append(bit)
    if rl_bits:
        segments.append((PRIO_RATELIMIT, GRP_BUDGET, " ".join(rl_bits)))

    # ── Rest reminder (hidden until you've been at it a while) ──
    if enabled("REST"):
        worked = rest_minutes()
        if worked is not None:
            seg = rest_segment(worked)
            if seg:
                segments.append((rest_priority(worked), GRP_REST, seg))

    sys.stdout.write(fit(segments, term_width() - 2))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never let the status line die — minimal safe fallback.
        sys.stdout.write("\033[38;5;209m✻ Claude\033[0m")
