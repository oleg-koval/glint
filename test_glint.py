#!/usr/bin/env python3
"""Smoke + unit tests for glint. Stdlib only: `python3 test_glint.py`."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import glint

HERE = Path(__file__).parent


def render(payload: dict) -> str:
    """Run glint.py as Claude Code would and return its stdout."""
    p = subprocess.run(
        [sys.executable, str(HERE / "glint.py")],
        input=json.dumps(payload), capture_output=True, text=True,
    )
    assert p.returncode == 0, p.stderr
    return p.stdout


# ── pure helpers ──────────────────────────────────────────────────────────────

def test_model_short():
    assert glint.model_short("Claude Sonnet 4.6") == "Sonnet 4.6"
    assert glint.model_short("Opus 4.8") == "Opus 4.8"

def test_tok_h():
    assert glint.tok_h(355_011) == "355k"
    assert glint.tok_h(1_000_000) == "1.0M"
    assert glint.tok_h(42) == "42"

def test_gauge_bounds():
    assert glint.gauge(0.0) == "▕░░░░░░░░▏"
    assert glint.gauge(1.0) == "▕████████▏"
    assert glint.gauge(0.5).count("█") == 4

def test_human_dur():
    assert glint.human_dur(30_000) == "30s"
    assert glint.human_dur(244_000) == "4m"
    assert glint.human_dur(3_660_000) == "1h01m"


# ── context_tokens reads main-thread assistant turns only ─────────────────────

def test_context_tokens_skips_sidechain():
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps({"type": "assistant", "isSidechain": True,
                            "message": {"usage": {"cache_read_input_tokens": 999_999}}}) + "\n")
        f.write(json.dumps({"type": "assistant",
                            "message": {"usage": {"input_tokens": 10,
                                                  "cache_creation_input_tokens": 5,
                                                  "cache_read_input_tokens": 100}}}) + "\n")
        path = f.name
    assert glint.context_tokens(path) == 115  # main-thread turn, not the sidechain

def test_context_tokens_missing_file():
    assert glint.context_tokens("/nope/does/not/exist.jsonl") == 0


# ── end-to-end render, graceful degradation ───────────────────────────────────

def test_full_payload_renders_all_segments():
    out = render({
        "model": {"display_name": "Claude Sonnet 4.6"},
        "cwd": str(HERE),
        "cost": {"total_cost_usd": 0.42, "total_duration_ms": 244_000,
                 "total_lines_added": 1247, "total_lines_removed": 340},
    })
    assert "Sonnet 4.6" in out
    assert "$0.42" in out
    assert "+1.2k" in out

def test_minimal_payload_still_has_model():
    out = render({"model": {"display_name": "Opus 4.8"}, "cwd": "/tmp"})
    assert "Opus 4.8" in out

def test_garbage_stdin_does_not_crash():
    p = subprocess.run([sys.executable, str(HERE / "glint.py")],
                       input="not json", capture_output=True, text=True)
    assert p.returncode == 0
    assert "Claude" in p.stdout

def test_cost_hidden_when_absent():
    out = render({"model": {"display_name": "Haiku 4.5"}, "cwd": "/tmp"})
    assert "$" not in out


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
