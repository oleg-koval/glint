#!/usr/bin/env python3
"""glint-alert — a fail-safe compaction alarm for Claude Code.

Companion Stop hook to the glint status line. glint *shows* your context gauge;
this *tells* you: a macOS notification + sound the moment context crosses 75%,
then once more at 90% — so you compact deliberately instead of getting surprised
by auto-compact. It also prints an inline reminder of exactly what to run.

Wire it in ~/.claude/settings.json under hooks.Stop:

    "hooks": {
      "Stop": [
        { "hooks": [ { "type": "command",
            "command": "python3 \\"/Users/you/.claude/glint_alert.py\\"" } ] }
      ]
    }

Reads the Stop hook JSON on stdin. Never blocks the turn and never raises — any
failure just means no alert. Debounced per session so it fires once per tier.
Set GLINT_ALERT_SILENT=1 to suppress the OS notification (keeps the inline note).

Repo: https://github.com/oleg-koval/glint
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

WARN_PCT = 75   # first nudge — still plenty of runway to compact cleanly
CRIT_PCT = 90   # last call before built-in auto-compact takes over at the ceiling


def context_tokens(transcript_path: str) -> tuple[int, str]:
    """(total input-side tokens, model id) of the last main-thread assistant turn.

    Mirrors glint.context_usage; kept standalone so the alarm still works when
    glint.py isn't installed alongside it. (0, "") when unavailable.
    """
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, 2)  # EOF
            pos = f.tell()
            lines = []
            chunk_size = 8192
            while pos > 0 and len(lines) < 100:
                read_size = min(chunk_size, pos)
                pos -= read_size
                f.seek(pos)
                chunk = f.read(read_size)
                lines = chunk.split(b"\n") + lines
                if len(lines) > 100:
                    lines = lines[-100:]
    except Exception:
        return 0, ""
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        # Only main-thread assistant turns reflect real context — skip sub-agent
        # (sidechain) usage so a delegate's size never triggers a false alarm.
        if not isinstance(o, dict):
            continue
        if o.get("type") != "assistant" or o.get("isSidechain"):
            continue
        msg = o.get("message")
        if not isinstance(msg, dict):
            msg = {}
        u = msg.get("usage") or o.get("usage")
        if not isinstance(u, dict):
            continue
        if u:
            try:
                input_tok = u.get("input_tokens", 0)
                cache_create = u.get("cache_creation_input_tokens", 0)
                cache_read = u.get("cache_read_input_tokens", 0)
                total = (
                    int(input_tok if isinstance(input_tok, (int, float)) else 0)
                    + int(cache_create if isinstance(cache_create, (int, float)) else 0)
                    + int(cache_read if isinstance(cache_read, (int, float)) else 0)
                )
            except (ValueError, TypeError):
                continue
            model = msg.get("model") or o.get("model") or ""
            return total, model
    return 0, ""


def window_size(tokens: int, model: str, data: dict) -> int:
    """Best-effort context-window limit. Trust a reported size; otherwise guess
    1M for [1m] models / >200k sessions, else 200k. Mirrors glint's logic."""
    cw = data.get("context_window") or {}
    limit = cw.get("context_window_size")
    if isinstance(limit, (int, float)) and limit > 0:
        return int(limit)
    if re.search(r"1m|\[1m\]", model or "", re.I) or tokens > 200_000 or data.get("exceeds_200k_tokens"):
        return 1_000_000
    return 200_000


def tier_for(pct: float) -> int:
    """Alert tier for a usage percentage: 90, 75, or 0 (no alert)."""
    return CRIT_PCT if pct >= CRIT_PCT else WARN_PCT if pct >= WARN_PCT else 0


def notify(title: str, body: str) -> None:
    """macOS notification + sound. No-op off macOS, when GLINT_ALERT_SILENT is
    set, or if osascript fails — never raises."""
    if sys.platform != "darwin" or os.environ.get("GLINT_ALERT_SILENT"):
        return
    script = (
        f"display notification {json.dumps(body, ensure_ascii=False)} "
        f"with title {json.dumps(title, ensure_ascii=False)} sound name \"Submarine\""
    )
    try:
        subprocess.run(
            ["osascript", "-e", script], timeout=4,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def messages(pct: int, tokens: int, limit: int, tier: int) -> tuple[str, str, str]:
    """(notification title, notification body, inline CLI message)."""
    used = f"{tokens:,}/{limit:,} tokens"
    title = f"🛑 Context {pct}% — compact now" if tier >= CRIT_PCT else f"⚠️ Context {pct}%"
    body = f"{used}. Type /compact in the Claude Code prompt to free the window."
    cli = (
        f"{title} ({used}).\n"
        "→ WHERE: type this in the Claude Code prompt box (not the shell):\n"
        "    /compact                 — summarize the conversation, free the window\n"
        "    /compact keep <what>     — same, but tell it what to preserve\n"
        "WHAT it does: replaces the back-and-forth so far with a shorter summary; "
        "open files & recent work are kept. Built-in auto-compact still backstops near 100%."
    )
    return title, body, cli


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return
    if not isinstance(data, dict):
        return
    tokens, model = context_tokens(data.get("transcript_path") or "")
    if tokens <= 0:
        return
    limit = window_size(tokens, model, data)
    pct_raw = tokens / limit * 100
    tier = tier_for(pct_raw)
    pct = round(pct_raw)
    if tier == 0:
        return

    # Debounce: alert only when crossing into a higher tier than seen this session.
    sid = data.get("session_id") or "default"
    flag = os.path.join(tempfile.gettempdir(), f"glint-alert-{sid}.json")
    last = 0
    try:
        with open(flag) as f:
            last = int((json.load(f) or {}).get("tier", 0))
    except Exception:
        pass
    if tier <= last:
        return
    try:
        with open(flag, "w") as f:
            json.dump({"tier": tier}, f)
    except Exception:
        pass

    title, body, cli = messages(pct, tokens, limit, tier)
    notify(title, body)
    # systemMessage surfaces inline in the CLI; it does not block Stop.
    sys.stdout.write(json.dumps({"systemMessage": cli}))


if __name__ == "__main__":
    main()
