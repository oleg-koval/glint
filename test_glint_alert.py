#!/usr/bin/env python3
"""Smoke + unit tests for glint_alert. Stdlib only: `python3 test_glint_alert.py`.

Runs the hook the way Claude Code's Stop event would (subprocess, JSON on stdin)
with GLINT_ALERT_SILENT=1 so no real OS notification fires during the run.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import glint_alert

HERE = Path(__file__).parent


def run(payload: dict) -> str:
    """Run glint_alert.py as the Stop hook would; return stdout (silenced OS notif)."""
    env = {**os.environ, "GLINT_ALERT_SILENT": "1"}
    p = subprocess.run(
        [sys.executable, str(HERE / "glint_alert.py")],
        input=json.dumps(payload), capture_output=True, text=True, env=env,
    )
    assert p.returncode == 0, p.stderr
    return p.stdout


def transcript(tokens: int, model: str = "claude-opus-4-8[1m]", sidechain=False) -> str:
    """Write a one-line transcript whose last assistant turn totals `tokens`."""
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    row = {
        "type": "assistant", "isSidechain": sidechain,
        "message": {"role": "assistant", "model": model,
                    "usage": {"input_tokens": tokens, "cache_read_input_tokens": 0,
                              "cache_creation_input_tokens": 0}},
    }
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(row) + "\n")
    return path


# ── pure helpers ──────────────────────────────────────────────────────────────

def test_tier_for_thresholds():
    assert glint_alert.tier_for(0) == 0
    assert glint_alert.tier_for(74) == 0
    assert glint_alert.tier_for(75) == 75
    assert glint_alert.tier_for(89) == 75
    assert glint_alert.tier_for(90) == 90
    assert glint_alert.tier_for(100) == 90


def test_window_size_reported_wins():
    assert glint_alert.window_size(50_000, "", {"context_window": {"context_window_size": 400_000}}) == 400_000


def test_window_size_guesses_1m_for_1m_model():
    assert glint_alert.window_size(10_000, "claude-opus-4-8[1m]", {}) == 1_000_000


def test_window_size_guesses_1m_when_over_200k():
    assert glint_alert.window_size(250_000, "claude-sonnet-5", {}) == 1_000_000


def test_window_size_defaults_to_200k():
    assert glint_alert.window_size(50_000, "claude-sonnet-5", {}) == 200_000


def test_context_tokens_sums_all_input_tiers():
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps({"type": "assistant", "message": {"role": "assistant",
                "model": "m", "usage": {"input_tokens": 100,
                "cache_read_input_tokens": 20, "cache_creation_input_tokens": 5}}}) + "\n")
    tokens, model = glint_alert.context_tokens(path)
    os.unlink(path)
    assert tokens == 125 and model == "m"


def test_context_tokens_skips_sidechain():
    path = transcript(800_000, sidechain=True)
    tokens, _ = glint_alert.context_tokens(path)
    os.unlink(path)
    assert tokens == 0


def test_context_tokens_missing_file():
    assert glint_alert.context_tokens("/no/such/file.jsonl") == (0, "")


def test_messages_mention_compact_and_where():
    _, _, cli = glint_alert.messages(82, 820_000, 1_000_000, 75)
    assert "/compact" in cli
    assert "prompt box" in cli
    assert "820,000/1,000,000" in cli


# ── end-to-end via subprocess ───────────────────────────────────────────────────

def test_warn_tier_emits_system_message():
    path = transcript(820_000)  # 82% of 1M
    try:
        out = run({"transcript_path": path, "session_id": "t-warn"})
    finally:
        os.unlink(path)
        _cleanup("t-warn")
    msg = json.loads(out)["systemMessage"]
    assert "82%" in msg and "/compact" in msg


def test_below_threshold_is_silent():
    path = transcript(400_000)  # 40% of 1M
    try:
        out = run({"transcript_path": path, "session_id": "t-low"})
    finally:
        os.unlink(path)
    assert out == ""


def test_debounce_same_tier_second_run_silent():
    path = transcript(800_000)  # 80% → warn tier
    try:
        first = run({"transcript_path": path, "session_id": "t-dbnc"})
        second = run({"transcript_path": path, "session_id": "t-dbnc"})
    finally:
        os.unlink(path)
        _cleanup("t-dbnc")
    assert first != "" and second == ""


def test_escalation_warn_then_crit_alerts_again():
    warn = transcript(800_000)   # 80% → warn
    crit = transcript(950_000)   # 95% → crit
    try:
        a = run({"transcript_path": warn, "session_id": "t-esc"})
        b = run({"transcript_path": crit, "session_id": "t-esc"})
    finally:
        os.unlink(warn)
        os.unlink(crit)
        _cleanup("t-esc")
    assert a != "" and b != ""
    assert "compact now" in json.loads(b)["systemMessage"]


def test_garbage_stdin_does_not_crash():
    p = subprocess.run(
        [sys.executable, str(HERE / "glint_alert.py")],
        input="not json{", capture_output=True, text=True,
        env={**os.environ, "GLINT_ALERT_SILENT": "1"},
    )
    assert p.returncode == 0
    assert p.stdout == ""


def test_missing_transcript_is_silent():
    out = run({"session_id": "t-none"})
    assert out == ""


def _cleanup(sid: str) -> None:
    flag = os.path.join(tempfile.gettempdir(), f"glint-alert-{sid}.json")
    try:
        os.unlink(flag)
    except OSError:
        pass


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ✓ {name}")
            except AssertionError as e:
                failed += 1
                print(f"  ✗ {name}: {e}")
    if failed:
        print(f"\n{failed} failed")
        sys.exit(1)
    print("\nall passed")
