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

__version__ = "1.3.0"   # keep in step with CHANGELOG.md and the git tag

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
try:
    import fcntl
except ImportError:
    fcntl = None  # Windows doesn't have fcntl; locking is best-effort

# ── 256-color palette ─────────────────────────────────────────────────────────
CORAL = 209      # model badge (Anthropic-ish)
BLUE = 75        # directory
GREEN = 114      # git clean / cheap / added
YELLOW = 179     # git dirty / mid cost
RED = 174        # behind / expensive / removed
GOLD = 220       # cost
DIM = 244        # separators, labels
PURPLE = 141     # worktree
CYAN = 80        # hydration
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
PRIO_WATER = 5.5     # gentler than the break nudge, drops before it

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


# Codepoints below the emoji planes that terminals still draw double-wide, i.e.
# Emoji_Presentation=Yes in UTS #51 — plus the clock/media block glint itself
# uses (⏱ ⏩), which most terminals widen. Blanket-widening 0x2600-0x27BF was
# wrong: ✻ (U+273B) in the model badge and ⚠ (U+26A0) in the context warning are
# one cell each, and counting them as two made every line measure too wide, so
# `fit` dropped segments the terminal had room for.
_WIDE = (
    (0x1F000, 0x1FAFF), (0x231A, 0x231B), (0x23E9, 0x23F3), (0x25FD, 0x25FE),
    (0x2614, 0x2615), (0x2648, 0x2653), (0x267F, 0x267F), (0x2693, 0x2693),
    (0x26A1, 0x26A1), (0x26AA, 0x26AB), (0x26BD, 0x26BE), (0x26C4, 0x26C5),
    (0x26CE, 0x26CE), (0x26D4, 0x26D4), (0x26EA, 0x26EA), (0x26F2, 0x26F3),
    (0x26F5, 0x26F5), (0x26FA, 0x26FA), (0x26FD, 0x26FD), (0x2705, 0x2705),
    (0x270A, 0x270B), (0x2728, 0x2728), (0x274C, 0x274C), (0x274E, 0x274E),
    (0x2753, 0x2755), (0x2757, 0x2757), (0x2795, 0x2797), (0x27B0, 0x27B0),
    (0x27BF, 0x27BF), (0x2B1B, 0x2B1C), (0x2B50, 0x2B50), (0x2B55, 0x2B55),
)


def vis_width(s: str) -> int:
    """Rendered cell width: ANSI is invisible, emoji take two cells.

    A glyph counts as wide if it's emoji-by-default or carries a U+FE0F
    presentation selector (♻️ is two cells, ♻ on its own is one).
    """
    text = _OSC8.sub("", _ANSI.sub("", s))
    w = 0
    for i, ch in enumerate(text):
        o = ord(ch)
        if o == 0xFE0F or unicodedata.combining(ch):
            continue                      # variation selector / combining mark
        emoji_vs = text[i + 1:i + 2] == "\ufe0f"
        if emoji_vs or any(lo <= o <= hi for lo, hi in _WIDE):
            w += 2
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


def fit(segments: list[tuple[float, int, str]], width: int) -> str:
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


def _safe_url(url) -> str:
    """An https URL, or "" — a cache file is not something to trust blindly.

    The PR cache lives in a shared temp dir under a predictable name, so another
    local user could plant one. An OSC 8 target is invisible in the terminal, and
    a link that lies about where it goes is exactly the sort of thing worth not
    rendering.
    """
    return url if isinstance(url, str) and url.startswith("https://") and "\n" not in url else ""


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


def _uid() -> str:
    """A stable per-user tag for temp file names.

    `os.getuid` is Unix-only. Without this, every cache path raised on Windows
    and the whole line collapsed to the bare `✻ Claude` fallback — the status
    line worked everywhere except the platform that never got tested.
    """
    getuid = getattr(os, "getuid", None)
    if getuid is not None:
        return str(getuid())
    return re.sub(r"\W", "", os.environ.get("USERNAME") or os.environ.get("USER") or "user")[:32] or "user"


def _write_private(path: str, payload: dict) -> None:
    """Write JSON to `path` atomically, readable only by us.

    O_EXCL on the temp name and 0600 on the mode keep a shared temp dir from
    being a way to hand us someone else's content.
    """
    tmp = f"{path}.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_own_json(path: str):
    """Parsed JSON from `path`, but only if we own it and it's a plain file."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PermissionError(f"{path} is not a regular file")
        # Windows reports st_uid == 0 for everything, so there's nothing to
        # compare against; the per-user temp dir is the protection there.
        getuid = getattr(os, "getuid", None)
        if getuid is not None and st.st_uid != getuid():
            raise PermissionError(f"{path} is not ours")
        with os.fdopen(fd, "r") as fh:
            return json.load(fh)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _pr_cache_path(repo_root: str, branch: str) -> str:
    key = hashlib.sha1(f"{repo_root}\0{branch}".encode()).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), f"glint-pr-{_uid()}-{key}.json")


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

    try:
        _write_private(cache, {"at": time.time(), "pr": rows[0] if rows else None})
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
        payload = _load_own_json(cache)
        age = time.time() - float(payload.get("at") or 0)
    except Exception:
        payload = None

    ttl = _PR_TTL if (payload or {}).get("pr") else _PR_NEGATIVE_TTL
    if payload is None or age is None or age > ttl:
        if os.environ.get("GLINT_PR_SYNC"):          # tests want a deterministic result
            _pr_refresh(cache, dir_, branch)
            try:
                payload = _load_own_json(cache)
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
    out = "⇄ " + c(link(label, _safe_url(pr.get("url"))), DIM if pr.get("isDraft") else BLUE)
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
    # Codex model ids look like "gpt-5.6-sol": keep the codename, since that is
    # the part that distinguishes them, and drop the vendor prefix.
    if low.startswith("gpt-"):
        return "G" + n[4:]
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
WATER_EVERY = 45.0   # a glass roughly every 45 min spreads ~2L over a working day


BRANCH_MAX = 28      # cells a branch name may occupy before it gets elided


def shorten_branch(name: str, limit: int | None = None) -> str:
    """Elide the middle of a long branch name, keeping both ends.

    A ticket-prefixed branch carries its meaning at the front (`dubo-175`) and
    its subject at the back (`kill-gic`); it's the words in between you can
    afford to lose. Printing it whole pushed the dirty count and ahead/behind
    markers off the line, which is the part you actually watch while working.
    """
    limit = int(env_minutes("BRANCH_MAX", BRANCH_MAX)) if limit is None else limit
    limit = max(limit, 8)                    # below this there is nothing left to read
    if vis_width(name) <= limit:
        return name
    # Measure by display-cell width and select head/tail portions that fit.
    keep = limit - 1                         # the ellipsis costs one cell
    head_budget = (keep + 1) // 2            # odd budgets favour the ticket prefix
    tail_budget = keep - head_budget
    # Find the longest prefix that fits head_budget cells.
    head_chars = 0
    for i in range(len(name)):
        if vis_width(name[:i+1]) > head_budget:
            break
        head_chars = i + 1
    # Find the longest suffix that fits tail_budget cells.
    tail_chars = 0
    if tail_budget > 0:
        for i in range(len(name) - 1, -1, -1):
            if vis_width(name[i:]) > tail_budget:
                break
            tail_chars = len(name) - i
    return name[:head_chars] + "…" + (name[len(name) - tail_chars:] if tail_chars else "")


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
        tempfile.gettempdir(), f"glint-rest-{_uid()}.json"
    )


class _FileLock:
    """Context manager for advisory file locking. Best-effort on Windows."""
    def __init__(self, path: str):
        self.path = path
        self.lock_path = path + ".lock"
        self.fd = None

    def __enter__(self):
        try:
            self.fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            if fcntl is not None:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            pass  # best-effort locking
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.fd is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
                os.close(self.fd)
            except Exception:
                pass


def clocks(now: float | None = None, path: str | None = None, write: bool = True):
    """Both body clocks: minutes worked, and minutes since a drink.

    One function because they share one state file; two writers would race each
    other. Returns `{"work": mins, "water": mins}`, or None if the file can't be
    used. A break (or `--rested`) resets both, on the assumption that getting up
    is when you refill the glass; `--drank` resets water alone.
    """
    now = time.time() if now is None else now
    path = path or _rest_state_path()
    gap = env_minutes("REST_GAP", REST_GAP) * 60

    with _FileLock(path):
        start = water = now
        try:
            prev = _load_own_json(path)
            last, prev_start = float(prev["last"]), float(prev["start"])
            # Clock skew or a stale file from a past boot: treat as a fresh start.
            if 0 <= now - last < gap and prev_start <= now:
                start = prev_start
                prev_water = float(prev.get("water", now))
                water = prev_water if 0 <= now - prev_water else now
        except Exception:
            pass

        if write:
            try:
                _write_private(path, {"start": start, "last": now, "water": water})
            except Exception:
                return None

    return {"work": (now - start) / 60, "water": (now - water) / 60}


def rest_minutes(now: float | None = None, path: str | None = None, write: bool = True):
    """Minutes worked without a break, tracking state across renders.

    Claude Code renders on activity, so the gap between two renders is idle time.
    A gap of `GLINT_REST_GAP` minutes or more counts as a real break and restarts
    the clock — which is also the feedback that the break landed, since the
    segment disappears. Returns None if the state file can't be used; a status
    line is never worth an error.

    `write=False` reads without stamping `last`. Only a render may stamp it: if
    merely asking "how long have I been at it?" counted as activity, checking the
    clock and then stepping away for nine minutes would lose the break the
    previous render would otherwise have detected.
    """
    both = clocks(now=now, path=path, write=write)
    return None if both is None else both["work"]


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
    with _FileLock(path):
        try:
            _write_private(path, {"start": now, "last": now, "water": now})
            return True
        except Exception:
            return False


def water_reset(path: str | None = None) -> bool:
    """Refill the glass without claiming you took a break. True if it stuck."""
    now = time.time()
    path = path or _rest_state_path()
    with _FileLock(path):
        try:
            prev = _load_own_json(path)
            start = float(prev["start"])
        except Exception:
            start = now
        try:
            _write_private(path, {"start": start, "last": now, "water": now})
            return True
        except Exception:
            return False


def water_segment(mins: float) -> str:
    """The hydration nudge, or "" while the glass is recent enough."""
    every = env_minutes("WATER_EVERY", WATER_EVERY)
    if mins < every:
        return ""
    clock = f"{int(mins)}m" if mins < 60 else f"{int(mins // 60)}h{int(mins % 60):02d}m"
    if mins >= every * 2:
        return "💧 " + c(clock, CYAN, bold=True) + c(" drink", CYAN)
    return c(f"💧 {clock}", CYAN)


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


USAGE = (
    "usage: glint.py [--harness claude|codex] [--tmux] [--width N]\n"
    "       glint.py --rested | --drank | --rest-status | --version\n"
    "       (no arguments: reads Claude Code status JSON on stdin)"
)


def codex_home() -> str:
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def _tail_text(path: str, max_bytes: int = 256 * 1024) -> str:
    """The last `max_bytes` of a file, starting at a line boundary.

    A long Codex session runs to megabytes and we want the newest records, so
    reading it whole on every repaint would be the slow thing in the render.
    """
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - max_bytes))
        chunk = fh.read()
    if size > max_bytes:
        chunk = chunk.split(b"\n", 1)[-1]       # drop the partial first line
    return chunk.decode("utf-8", "replace")


def _records(text: str):
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                yield json.loads(line)
            except Exception:
                continue


def _codex_sessions(limit: int = 8) -> list[str]:
    """Rollout logs, newest first. Only the few newest can matter."""
    root = os.path.join(codex_home(), "sessions")
    found = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.startswith("rollout-") and name.endswith(".jsonl"):
                path = os.path.join(dirpath, name)
                try:
                    found.append((os.path.getmtime(path), path))
                except OSError:
                    continue
    found.sort(reverse=True)
    return [p for _mtime, p in found[:limit]]


def codex_payload(cwd: str | None = None) -> dict:
    """Newest Codex session, translated into Claude Code's payload shape.

    Codex has no status-line hook (its own bar takes a fixed list of built-in
    items), but it writes everything worth showing to
    `~/.codex/sessions/<date>/rollout-*.jsonl`: token counts against the model
    window, the cached share of them, and quota windows with reset times. So the
    adapter reads that and every segment downstream stays unchanged.
    """
    cwd = cwd or os.getcwd()
    payload: dict = {"cwd": cwd}

    chosen = None
    for path in _codex_sessions():
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                head = list(_records(fh.read(65536)))
        except OSError:
            continue
        meta = next(((r.get("payload") or {}) for r in head
                     if r.get("type") == "session_meta"), {})
        session_cwd = meta.get("cwd") or ""
        if chosen is None:
            chosen = (path, head)                # newest, as the fallback
        if session_cwd and (cwd == session_cwd or cwd.startswith(session_cwd.rstrip("/") + "/")):
            chosen = (path, head)                # a session rooted at this tree wins
            break
    if chosen is None:
        return payload

    path, head = chosen
    records = head + list(_records(_tail_text(path)))

    model = effort = ""
    usage: dict = {}
    limits: dict = {}
    for rec in records:                          # last write wins: newest state
        body = rec.get("payload") or {}
        if body.get("type") == "turn_context" or rec.get("type") == "turn_context":
            model = body.get("model") or model
            effort = body.get("effort") or effort
        if body.get("type") == "token_count":
            info = body.get("info") or {}
            usage = info or usage
            limits = body.get("rate_limits") or limits

    if model:
        payload["model"] = {"display_name": model, "id": model}
    if effort:
        payload["effort"] = {"level": effort}

    last = (usage.get("last_token_usage") or {}) if usage else {}
    window = usage.get("model_context_window") if usage else None
    if isinstance(window, (int, float)) and window > 0 and last:
        payload["context_window"] = {
            "total_input_tokens": last.get("total_tokens") or 0,
            "context_window_size": window,
        }
    if last.get("input_tokens"):
        payload["cache"] = {"total": last["input_tokens"],
                            "read": last.get("cached_input_tokens") or 0}

    # Codex reports quota as primary/secondary with the window length attached,
    # rather than named five_hour/seven_day slots, so bucket by that length.
    rl = {}
    for key in ("primary", "secondary"):
        entry = (limits or {}).get(key)
        if not isinstance(entry, dict) or not isinstance(entry.get("used_percent"), (int, float)):
            continue
        minutes = entry.get("window_minutes") or 0
        slot = "five_hour" if 0 < minutes <= 720 else "seven_day"
        rl[slot] = {"used_percentage": entry["used_percent"], "resets_at": entry.get("resets_at")}
    if rl:
        payload["rate_limits"] = rl
    return payload


_SGR = re.compile(r"\033\[(?:(1);)?38;5;(\d+)m")


def to_tmux(line: str) -> str:
    """Rewrite our ANSI colours as tmux format strings.

    tmux status strings take `#[fg=colour114]`, not SGR escapes, and treat a bare
    `#` as the start of a format, so literal ones have to be doubled first.
    Hyperlinks go: OSC 8 does not survive a status line.
    """
    line = _OSC8.sub("", line).replace("#", "##")
    line = _SGR.sub(lambda m: "#[fg=colour" + m.group(2) + (",bold]" if m.group(1) else "]"), line)
    return line.replace("\033[0m", "#[default]")


def main() -> None:
    # Detached background refresh spawned by pr_for_branch; not a status-line render.
    if len(sys.argv) == 5 and sys.argv[1] == "--refresh-pr":
        _pr_refresh(sys.argv[2], sys.argv[3], sys.argv[4])
        return

    # Claude Code always calls this with JSON on stdin and no arguments, so these
    # flags are free for humans. `--rested` is how you tell it the break happened.
    if len(sys.argv) == 2 and sys.argv[1] in ("--rested", "--rest-reset"):
        ok = rest_reset()
        print("☕ break logged: work clock and water back to zero" if ok
              else "couldn't write the rest clock; nothing changed")
        sys.exit(0 if ok else 1)
    if len(sys.argv) == 2 and sys.argv[1] in ("--drank", "--hydrated", "--water"):
        ok = water_reset()
        print("💧 noted: water clock back to zero" if ok
              else "couldn't write the water clock; nothing changed")
        sys.exit(0 if ok else 1)
    if len(sys.argv) == 2 and sys.argv[1] in ("--version", "-V"):
        print(f"glint {__version__}")
        return
    if len(sys.argv) == 2 and sys.argv[1] == "--rest-status":
        both = clocks(write=False)            # reading the clocks is not activity
        if both is None:
            print("rest clock unavailable")
            sys.exit(1)
        print(f"{int(both['work'])} min since your last break "
              f"(nudge at {int(env_minutes('REST_NUDGE', REST_NUDGE))})")
        print(f"{int(both['water'])} min since your last drink "
              f"(nudge at {int(env_minutes('WATER_EVERY', WATER_EVERY))})")
        return

    harness, tmux, width = "claude", False, None
    args = sys.argv[1:]
    while args:
        a = args.pop(0)
        if a == "--harness" and args:
            harness = args.pop(0)
        elif a == "--tmux":
            tmux = True
        elif a == "--width" and args:
            try:
                width = int(args.pop(0))
            except ValueError:
                print("glint: --width wants a number", file=sys.stderr)
                sys.exit(2)
        else:
            print(f"glint: unknown option {a!r}\n{USAGE}", file=sys.stderr)
            sys.exit(2)

    if harness in ("claude", "claude-code"):
        try:
            d = json.loads(sys.stdin.read())
        except Exception:
            d = {}
    elif harness == "codex":
        d = codex_payload()          # no hook to pipe us anything: read its session log
    else:
        print(f"glint: unknown harness {harness!r} (try: claude, codex)", file=sys.stderr)
        sys.exit(2)

    if width is None:
        # A tmux status has no tty to measure, so assume it is not the constraint
        # and let tmux itself truncate; a real terminal gets the honest width.
        width = 400 if tmux else term_width() - 2
    line = build_line(d, width)
    sys.stdout.write(to_tmux(line) if tmux else line)


def build_line(d: dict, width: int) -> str:
    """The status line itself, from a payload in Claude Code's shape."""
    segments: list[tuple[float, int, str]] = []   # (priority, group, text); see fit()

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
        g = "🌿 " + c(shorten_branch(branch), color)
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
    # An adapter can hand us the numbers directly; only Claude Code has a
    # transcript file to derive them from.
    supplied = d.get("cache") or {}
    if isinstance(supplied.get("total"), (int, float)) and supplied["total"] > 0:
        tx_tokens, tx_cached = supplied["total"], supplied.get("read") or 0
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

    # ── Body clocks: break nudge, then hydration (both hidden until due) ──
    if enabled("REST") or enabled("WATER"):
        both = clocks()
        if both is not None:
            seg = rest_segment(both["work"]) if enabled("REST") else ""
            if seg:
                segments.append((rest_priority(both["work"]), GRP_REST, seg))
            wseg = water_segment(both["water"]) if enabled("WATER") else ""
            if wseg:
                segments.append((PRIO_WATER, GRP_REST, wseg))

    return fit(segments, width)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never let the status line die — minimal safe fallback.
        sys.stdout.write("\033[38;5;209m✻ Claude\033[0m")
