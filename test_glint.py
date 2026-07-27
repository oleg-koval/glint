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
    assert glint.model_short("Claude Sonnet 4.6") == "S4.6"
    assert glint.model_short("Opus 4.8") == "O4.8"
    assert glint.model_short("Sonnet 5") == "S5"
    assert glint.model_short("Haiku 4.5") == "H4.5"
    assert glint.model_short("Fable 5") == "F5"
    assert glint.model_short("claude-sonnet-4-6") == "S4.6"  # hyphenated id
    assert glint.model_short("MyCustomModel") == "MyCustomModel"  # unknown family kept as-is

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


# ── context_usage reads main-thread assistant turns only ──────────────────────

def test_context_usage_skips_sidechain():
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps({"type": "assistant", "isSidechain": True,
                            "message": {"usage": {"cache_read_input_tokens": 999_999}}}) + "\n")
        f.write(json.dumps({"type": "assistant",
                            "message": {"usage": {"input_tokens": 10,
                                                  "cache_creation_input_tokens": 5,
                                                  "cache_read_input_tokens": 100}}}) + "\n")
        path = f.name
    # main-thread turn, not the sidechain; second value is the cache-read share
    assert glint.context_usage(path) == (115, 100)

def test_context_usage_missing_file():
    assert glint.context_usage("/nope/does/not/exist.jsonl") == (0, 0)


# ── reset_eta parses epoch and ISO-8601, hides the past ────────────────────────

def test_reset_eta_iso_future():
    import datetime as dt
    t = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2, minutes=10)
    assert glint.reset_eta(t.isoformat()).startswith("2h")

def test_reset_eta_epoch_future():
    import time
    assert glint.reset_eta(time.time() + 120) == "1m"

def test_reset_eta_past_and_garbage_are_empty():
    assert glint.reset_eta(0) == ""
    assert glint.reset_eta("not-a-date") == ""
    assert glint.reset_eta(None) == ""

def test_seconds_until_past_and_garbage_are_none():
    assert glint.seconds_until(0) is None
    assert glint.seconds_until("nope") is None
    assert glint.seconds_until(None) is None


# ── end-to-end render, graceful degradation ───────────────────────────────────

def test_full_payload_renders_all_segments():
    out = render({
        "model": {"display_name": "Claude Sonnet 4.6"},
        "cwd": str(HERE),
        "cost": {"total_cost_usd": 0.42, "total_duration_ms": 244_000,
                 "total_lines_added": 1247, "total_lines_removed": 340},
    })
    assert "S4.6" in out
    assert "$0.42" in out
    assert "+1.2k" in out

def test_minimal_payload_still_has_model():
    out = render({"model": {"display_name": "Opus 4.8"}, "cwd": "/tmp"})
    assert "O4.8" in out

def test_garbage_stdin_does_not_crash():
    p = subprocess.run([sys.executable, str(HERE / "glint.py")],
                       input="not json", capture_output=True, text=True)
    assert p.returncode == 0
    assert "Claude" in p.stdout

def test_cost_hidden_when_absent():
    out = render({"model": {"display_name": "Haiku 4.5"}, "cwd": "/tmp"})
    assert "$" not in out


# ── context_window field takes priority over transcript parsing ───────────────

def test_context_window_field_used_when_present():
    out = render({
        "model": {"display_name": "Sonnet 5"}, "cwd": "/tmp",
        "context_window": {"total_input_tokens": 750_000, "context_window_size": 1_000_000,
                            "used_percentage": 75, "remaining_percentage": 25},
    })
    assert "75%" in out
    assert "750k/1.0M" in out

def test_headroom_countdown_shows_below_30pct_remaining():
    out = render({
        "model": {"display_name": "Sonnet 5"}, "cwd": "/tmp",
        "context_window": {"total_input_tokens": 700_000, "context_window_size": 1_000_000,
                            "used_percentage": 70, "remaining_percentage": 30},
    })
    assert "left" in out and "300k" in out

def test_headroom_countdown_hidden_above_30pct_remaining():
    out = render({
        "model": {"display_name": "Sonnet 5"}, "cwd": "/tmp",
        "context_window": {"total_input_tokens": 690_000, "context_window_size": 1_000_000,
                            "used_percentage": 69, "remaining_percentage": 31},
    })
    assert "left" not in out

def test_reported_window_size_wins_over_the_200k_guess():
    # used_percentage is null until the first assistant turn, but the reported
    # size is already authoritative — a 1M session must not be drawn against 200k.
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps({"type": "assistant",
                            "message": {"usage": {"input_tokens": 150_000}}}) + "\n")
        path = f.name
    out = render({
        "model": {"display_name": "Opus 4.8"}, "cwd": "/tmp", "transcript_path": path,
        "context_window": {"context_window_size": 1_000_000, "used_percentage": None,
                            "total_input_tokens": 0},
    })
    assert "150k/1.0M" in out
    assert "15%" in out

def test_window_size_guessed_when_not_reported():
    out = render({
        "model": {"display_name": "Sonnet 5"}, "cwd": "/tmp",
        "context_window": {"total_input_tokens": 50_000},
    })
    assert "50k/200k" in out


# ── effort ladder and fast mode ────────────────────────────────────────────────

def test_effort_badge_renders_each_level():
    for level, mark in (("low", "L"), ("medium", "M"), ("high", "H"),
                        ("xhigh", "X"), ("max", "MAX")):
        out = render({"model": {"display_name": "Opus 4.8"}, "cwd": "/tmp",
                      "effort": {"level": level}})
        assert mark in out, f"{level} → {mark}"

def test_effort_badge_hidden_when_model_has_no_effort():
    # absent means the model doesn't support effort — not that it's "high"
    out = render({"model": {"display_name": "Haiku 4.5"}, "cwd": "/tmp"})
    assert "H4.5 " not in out

def test_effort_badge_ignores_unknown_level():
    out = render({"model": {"display_name": "Opus 4.8"}, "cwd": "/tmp",
                  "effort": {"level": "turbo"}})
    assert "turbo" not in out

def test_fast_mode_marker_shows_and_hides():
    payload = {"model": {"display_name": "Opus 4.8"}, "cwd": "/tmp"}
    assert "⏩" in render({**payload, "fast_mode": True})
    assert "⏩" not in render({**payload, "fast_mode": False})
    assert "⏩" not in render(payload)


# ── pace: projected quota burn vs window elapsed ───────────────────────────────

def test_pace_on_track_when_burn_matches_clock():
    # half the 5h window left, half the quota used → exactly on pace
    assert abs(glint.pace(50, 2.5 * 3600, 5 * 3600) - 1.0) < 0.01

def test_pace_flags_burning_hot():
    # 80% used with half the window left → would need 160% of quota
    assert abs(glint.pace(80, 2.5 * 3600, 5 * 3600) - 1.6) < 0.01

def test_pace_none_when_window_too_young():
    # 4.9h left of a 5h window — too early to extrapolate
    assert glint.pace(1, 4.9 * 3600, 5 * 3600) is None

def test_pace_none_without_reset_time():
    assert glint.pace(50, None, 5 * 3600) is None

def test_pace_marker_renders_when_hot():
    import datetime as dt
    soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat()
    out = render({
        "model": {"display_name": "Sonnet 5"}, "cwd": "/tmp",
        "rate_limits": {"five_hour": {"used_percentage": 90, "resets_at": soon}},
    })
    assert "⚡" in out

def test_pace_marker_hidden_when_on_track():
    import datetime as dt
    soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat()
    out = render({
        "model": {"display_name": "Sonnet 5"}, "cwd": "/tmp",
        "rate_limits": {"five_hour": {"used_percentage": 50, "resets_at": soon}},
    })
    assert "⚡" not in out


# ── worktree segment ───────────────────────────────────────────────────────────

def test_worktree_shown_when_it_differs_from_directory():
    out = render({
        "model": {"display_name": "Sonnet 5"},
        "workspace": {"current_dir": "/tmp", "git_worktree": "/repos/feature-x"},
    })
    assert "feature-x" in out

def test_worktree_read_from_top_level_field():
    # Claude Code 2.1.x reports the worktree directly; no git call needed.
    out = render({
        "model": {"display_name": "Sonnet 5"},
        "workspace": {"current_dir": "/tmp"},
        "worktree": {"name": "feature-y", "path": "/repos/feature-y", "branch": "feature-y"},
    })
    assert "feature-y" in out

def test_worktree_hidden_when_same_as_directory():
    out = render({
        "model": {"display_name": "Sonnet 5"},
        "workspace": {"current_dir": "/tmp/thing", "git_worktree": "/tmp/thing"},
    })
    assert "🌳" not in out


# ── cache-hit ratio segment ────────────────────────────────────────────────────

def test_cache_ratio_from_transcript():
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps({"type": "assistant",
                            "message": {"usage": {"input_tokens": 10,
                                                  "cache_creation_input_tokens": 10,
                                                  "cache_read_input_tokens": 80}}}) + "\n")
        path = f.name
    out = render({"model": {"display_name": "Sonnet 5"}, "cwd": "/tmp",
                  "transcript_path": path})
    assert "80%" in out  # ♻️ cache-hit ratio

def test_cache_ratio_hidden_without_transcript():
    out = render({"model": {"display_name": "Sonnet 5"}, "cwd": "/tmp"})
    assert "♻" not in out


# ── rate limit bars (5h session / 7d weekly) ───────────────────────────────────

def test_rate_limits_render_when_present():
    out = render({
        "model": {"display_name": "Sonnet 5"}, "cwd": "/tmp",
        "rate_limits": {"five_hour": {"used_percentage": 63}, "seven_day": {"used_percentage": 10}},
    })
    assert "5h" in out and "63%" in out
    assert "7d" in out and "10%" in out

def test_rate_limits_hidden_when_absent():
    out = render({"model": {"display_name": "Sonnet 5"}, "cwd": "/tmp"})
    assert "5h" not in out and "7d" not in out


# ── narrow-window fitting ─────────────────────────────────────────────────────
def _seg(prio: int, width: int):
    """A segment of exactly `width` visible cells at the given priority."""
    return (prio, "x" * width)


def test_vis_width_ignores_ansi():
    assert glint.vis_width(glint.c("abc", glint.GREEN)) == 3


def test_vis_width_counts_emoji_as_two_cells():
    assert glint.vis_width("\U0001F4C1") == 2
    assert glint.vis_width("\u267B\ufe0f") == 2  # pictograph + variation selector


def test_fit_keeps_everything_when_it_already_fits():
    segs = [_seg(0, 5), _seg(3, 5), _seg(8, 5)]
    out = glint.fit(segs, 100)
    assert glint.vis_width(out) == 5 * 3 + 2 * 2  # three segments, two separators


def test_fit_drops_highest_priority_first():
    segs = [_seg(0, 10), _seg(2, 10), _seg(8, 10)]
    out = glint.fit(segs, 24)
    assert glint.vis_width(out) <= 24


def test_fit_never_drops_priority_zero():
    segs = [_seg(0, 30), _seg(1, 30), _seg(8, 30)]
    out = glint.fit(segs, 5)
    assert glint.vis_width(out) == 30  # clips rather than blanking the bar


def test_fit_readds_a_segment_that_still_fits():
    # Greedy removal drops the small (prio 8) before the large (prio 5); once the
    # large is gone the small fits again and must come back.
    segs = [_seg(0, 10), _seg(5, 40), _seg(8, 5)]
    out = glint.fit(segs, 20)
    assert glint.vis_width(out) == 10 + 2 + 5


def test_fit_preserves_original_segment_order():
    segs = [(0, "aaa"), (8, "bbb"), (2, "ccc")]
    plain = glint._ANSI.sub("", glint.fit(segs, 100))
    assert plain.index("aaa") < plain.index("bbb") < plain.index("ccc")


def test_term_width_falls_back_to_default_without_a_tty():
    p = subprocess.run(
        [sys.executable, "-c",
         "import glint; print(glint.term_width(default=77))"],
        capture_output=True, text=True, cwd=str(HERE),
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(HERE)},
        stdin=subprocess.DEVNULL,
    )
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == "77", p.stdout


def test_narrow_render_does_not_exceed_the_window():
    payload = {
        "model": {"display_name": "Opus 5"},
        "cwd": "/tmp",
        "context_window": {"used_percentage": 12, "context_window_size": 1_000_000,
                           "total_input_tokens": 115_000},
        "cost": {"total_cost_usd": 0.42, "total_duration_ms": 3_900_000,
                 "total_lines_added": 67, "total_lines_removed": 26},
    }
    for width in (30, 50, 80, 120):
        segs = [_seg(0, 7)] + [_seg(i, 20) for i in range(1, 6)]
        assert glint.vis_width(glint.fit(segs, width)) <= width, width
    assert "O5" in render(payload)  # still renders end-to-end


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
