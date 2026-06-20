#!/usr/bin/env python3
"""glint — a rich, fast, fail-safe status line for Claude Code.

Reads the Status hook JSON on stdin and prints one ANSI-colored line:

  ✻ Sonnet 4.6   📁 my-repo   🌿 main ●3 ↑1   💰 $0.42 · 4m   +1.2k/-340   🧠 36% ▕███░░░░░▏ 357k/1.0M

Every segment degrades gracefully: a missing field means the segment is omitted,
never an error. A crash falls back to a minimal badge so the bar never goes blank.

Repo: https://github.com/oleg-koval/glint
"""

from __future__ import annotations

import json
import os
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


def model_short(name: str) -> str:
    # "Claude Sonnet 4.6" → "Sonnet 4.6"; keep custom names as-is
    return name.replace("Claude ", "").strip() or name


def tok_h(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n // 1000}k"
    return str(n)


def context_tokens(transcript_path: str) -> int:
    """Current context size = input-side tokens of the most recent assistant turn.

    Reads the transcript JSONL backwards and returns the first usage block found
    on the main thread (input + cache_creation + cache_read). 0 if unavailable.
    """
    try:
        with open(transcript_path, "rb") as f:
            lines = f.readlines()
    except Exception:
        return 0
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
            return (
                int(u.get("input_tokens", 0))
                + int(u.get("cache_creation_input_tokens", 0))
                + int(u.get("cache_read_input_tokens", 0))
            )
    return 0


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
    tokens = context_tokens(d.get("transcript_path") or "")
    if tokens > 0:
        # 200k default; auto-bump to 1M when clearly on the long-context beta.
        limit = 1_000_000 if (tokens > 200_000 or d.get("exceeds_200k_tokens")) else 200_000
        pct = min(tokens / limit, 1.0)
        gc = GREEN if pct < 0.6 else YELLOW if pct < 0.85 else RED
        segments.append(
            "🧠 " + c(f"{pct * 100:.0f}%", gc, bold=True)
            + " " + c(gauge(pct), gc)
            + " " + c(f"{tok_h(tokens)}/{tok_h(limit)}", DIM)
        )

    sys.stdout.write(sep().join(segments))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never let the status line die — minimal safe fallback.
        sys.stdout.write("\033[38;5;209m✻ Claude\033[0m")
