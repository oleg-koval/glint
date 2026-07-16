#!/usr/bin/env python3
"""glint — a rich, fast, fail-safe status line for Claude Code.

Reads the Status hook JSON on stdin and prints one ANSI-colored line:

  ✻ F5   📁 my-repo   🌿 main ●3 ↑1   💰 $0.42 · 4m   +1.2k/-340   🧠 36% ▕███░░░░░▏ 357k/1.0M   ♻️ 94%   ⏱5h 63%▕███░░▏ ↻1h07m 📅7d 10%▕█░░░░▏ ↻2.3d

Every segment degrades gracefully: a missing field means the segment is omitted,
never an error. A crash falls back to a minimal badge so the bar never goes blank.

Repo: https://github.com/oleg-koval/glint
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

# ── 256-color palette ─────────────────────────────────────────────────────────
CORAL = 209      # model badge (Anthropic-ish)
BLUE = 75        # directory
GREEN = 114      # git clean / cheap / added
YELLOW = 179     # git dirty / mid cost
RED = 174        # behind / expensive / removed
GOLD = 220       # cost
DIM = 244        # separators, labels
PURPLE = 141     # reserved


def c(text: str, color: int, *, bold: bool = False) -> str:
    b = "1;" if bold else ""
    return f"\033[{b}38;5;{color}m{text}\033[0m"


def sep() -> str:
    return c("  ", DIM)


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


def main() -> None:
    raw = sys.stdin.read()
    try:
        d = json.loads(raw)
    except Exception:
        d = {}

    segments: list[str] = []

    # ── Model badge ──
    model = (d.get("model") or {}).get("display_name") or (d.get("model") or {}).get("id") or "Claude"
    segments.append(c("✻ ", CORAL, bold=True) + c(model_short(model), CORAL, bold=True))

    # ── Directory ──
    cwd = (d.get("workspace") or {}).get("current_dir") or d.get("cwd") or os.getcwd()
    home = os.path.expanduser("~")
    label = "~" if cwd == home else os.path.basename(cwd.rstrip("/")) or cwd
    segments.append("📁 " + c(label, BLUE))

    # ── Worktree ── (only when it adds information the directory segment lacks)
    wt = (d.get("workspace") or {}).get("git_worktree")
    if not wt:
        # Older Claude Code has no such field: a linked worktree is one whose
        # private git dir differs from the repo's common dir.
        gd, common = git(cwd, "rev-parse", "--absolute-git-dir"), git(cwd, "rev-parse", "--git-common-dir")
        if gd and common and os.path.abspath(gd) != os.path.abspath(common):
            wt = os.path.basename(git(cwd, "rev-parse", "--show-toplevel") or "")
    wt = os.path.basename(str(wt).rstrip("/")) if wt else ""
    if wt and wt != label:
        segments.append("🌳 " + c(wt, PURPLE))

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
        segments.append(g)

    # ── Cost · duration ──
    cost = d.get("cost") or {}
    money = cost.get("total_cost_usd")
    if isinstance(money, (int, float)) and money > 0:
        money_color = GREEN if money < 1 else GOLD if money < 5 else RED
        bits = ["💰 " + c(f"${money:.2f}", money_color)]
        dur = cost.get("total_duration_ms")
        if isinstance(dur, (int, float)) and dur > 0:
            bits.append(c(human_dur(dur), DIM))
        segments.append(c(" · ", DIM).join(bits))

    # ── Lines changed ──
    added = cost.get("total_lines_added")
    removed = cost.get("total_lines_removed")
    if added or removed:
        la = c(f"+{human_lines(added or 0)}", GREEN)
        lr = c(f"-{human_lines(removed or 0)}", RED)
        segments.append(f"{la}{c('/', DIM)}{lr}")

    # ── Live context gauge ──
    tx_tokens, tx_cached = context_usage(d.get("transcript_path") or "")
    cw = d.get("context_window") or {}
    used_pct = cw.get("used_percentage")
    limit = cw.get("context_window_size")
    if isinstance(used_pct, (int, float)) and limit:
        pct = min(max(used_pct / 100, 0.0), 1.0)
        tokens = cw.get("total_input_tokens") or tx_tokens
    else:
        tokens = tx_tokens
        # 200k default; auto-bump to 1M when clearly on the long-context beta.
        limit = 1_000_000 if (tokens > 200_000 or d.get("exceeds_200k_tokens")) else 200_000
        pct = min(tokens / limit, 1.0) if limit else 0.0

    if tokens > 0:
        gc = GREEN if pct < 0.6 else YELLOW if pct < 0.85 else RED
        seg = (
            "🧠 " + c(f"{pct * 100:.0f}%", gc, bold=True)
            + " " + c(gauge(pct), gc)
            + " " + c(f"{tok_h(tokens)}/{tok_h(limit)}", DIM)
        )
        # Approaching auto-compact: show how much runway is actually left.
        headroom = int(limit * (1 - pct))
        if pct >= 0.85:
            seg += "  " + c(f"⚠ {tok_h(headroom)} left", RED, bold=True)
        elif pct >= 0.7:
            seg += "  " + c(f"→ {tok_h(headroom)} left", YELLOW)
        segments.append(seg)

    # ── Prompt-cache efficiency (cache reads are ~10x cheaper than fresh input) ──
    if tx_tokens > 0:
        ratio = tx_cached / tx_tokens
        rc = GREEN if ratio >= 0.8 else YELLOW if ratio >= 0.5 else RED
        segments.append("♻️ " + c(f"{ratio * 100:.0f}%", rc))

    # ── Rate limit bars (5h session / 7d weekly) ──
    rl = d.get("rate_limits") or {}
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
            bit = f"{icon}{name} " + c(f"{used:.0f}%", rc) + c(gauge(rpct, width=5), rc)
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
        segments.append(" ".join(rl_bits))

    sys.stdout.write(sep().join(segments))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never let the status line die — minimal safe fallback.
        sys.stdout.write("\033[38;5;209m✻ Claude\033[0m")
