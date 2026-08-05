#!/usr/bin/env python3
"""Smoke + unit tests for glint. Stdlib only: `python3 test_glint.py`."""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import glint

HERE = Path(__file__).parent


def render(payload: dict, **env_overrides: str) -> str:
    """Run glint.py as Claude Code would and return its stdout.

    GLINT_* is scrubbed from the environment first: a developer with, say,
    `GLINT_COST=0` in their shell would otherwise fail tests that assert on the
    default line. Pass `GLINT_BARS="1"` explicitly to test a switched-on feature.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GLINT_")}
    # The rest clock is shared state in the temp dir; leave the developer's real
    # one alone unless a test opts in with its own GLINT_REST_STATE.
    env["GLINT_REST"] = "0"
    # Payloads whose cwd is a git repo would otherwise reach pr_for_branch, which
    # spawns a detached refresh plus a `gh` network call and writes the real
    # shared cache — orphan processes and state outside the test's control.
    env["GLINT_PR"] = "0"
    env.update(env_overrides)
    p = subprocess.run(
        [sys.executable, str(HERE / "glint.py")],
        input=json.dumps(payload), capture_output=True, text=True, env=env,
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


# ── opt-in gauges ─────────────────────────────────────────────────────────────

BARS_PAYLOAD = {
    "model": {"display_name": "Sonnet 5"}, "cwd": "/tmp",
    "context_window": {"used_percentage": 36, "context_window_size": 1_000_000,
                       "total_input_tokens": 357_000},
    "rate_limits": {"five_hour": {"used_percentage": 63}, "seven_day": {"used_percentage": 10}},
}


def test_opt_in_defaults_to_off():
    os.environ.pop("GLINT_NEWTHING", None)
    try:
        assert glint.opt_in("NEWTHING") is False
        for on in ("1", "true", "yes", "on", "ON"):
            os.environ["GLINT_NEWTHING"] = on
            assert glint.opt_in("NEWTHING") is True, on
        for off in ("0", "false", "no", "off", "", "maybe"):
            os.environ["GLINT_NEWTHING"] = off
            assert glint.opt_in("NEWTHING") is False, off
    finally:                     # a failed assert must not leak into later tests
        os.environ.pop("GLINT_NEWTHING", None)


def test_gauges_hidden_by_default_but_percentages_stay():
    out = render(BARS_PAYLOAD)
    assert "█" not in out and "░" not in out
    assert "36%" in out and "63%" in out and "10%" in out


def test_gauges_shown_when_bars_opted_in():
    out = render(BARS_PAYLOAD, GLINT_BARS="1")
    assert out.count("▕") == 3          # context + 5h + 7d
    assert "█" in out and "░" in out


# ── topic grouping ────────────────────────────────────────────────────────────

def test_groups_divided_by_rule():
    out = render({
        "model": {"display_name": "Sonnet 5"}, "cwd": "/tmp",
        "cost": {"total_cost_usd": 0.42, "total_lines_added": 10, "total_lines_removed": 2},
        "rate_limits": {"five_hour": {"used_percentage": 63}},
    })
    # session ┃ place ┃ change ┃ budget — three dividers, and none trailing.
    assert out.count("┃") == 3
    assert not glint._ANSI.sub("", out).rstrip().endswith("┃")


def test_same_group_segments_share_a_gap_not_a_rule():
    line = glint._ANSI.sub("", glint.fit([
        (glint.PRIO_DIR, glint.GRP_PLACE, "dir"),
        (glint.PRIO_GIT, glint.GRP_PLACE, "branch"),
    ], 80))
    assert line == "dir  branch"


def test_rule_sits_between_groups_only():
    line = glint._ANSI.sub("", glint.fit([
        (glint.PRIO_MODEL, glint.GRP_SESSION, "model"),
        (glint.PRIO_DIR, glint.GRP_PLACE, "dir"),
        (glint.PRIO_GIT, glint.GRP_PLACE, "branch"),
        (glint.PRIO_COST, glint.GRP_CHANGE, "cost"),
    ], 80))
    assert line == "model  ┃  dir  branch  ┃  cost"


def test_dropping_a_whole_group_drops_its_rule():
    # Only the model badge (priority 0) survives an 8-column window.
    line = glint._ANSI.sub("", glint.fit([
        (glint.PRIO_MODEL, glint.GRP_SESSION, "model"),
        (glint.PRIO_COST, glint.GRP_CHANGE, "cost"),
        (glint.PRIO_RATELIMIT, glint.GRP_BUDGET, "5h 63%"),
    ], 8))
    assert line == "model"


# ── rest reminder ─────────────────────────────────────────────────────────────

def _rest_state(tmp: str, start: float, last: float) -> None:
    with open(tmp, "w") as f:
        json.dump({"start": start, "last": last}, f)


def test_rest_clock_starts_at_zero():
    with tempfile.TemporaryDirectory() as d:
        state = str(Path(d) / "rest.json")
        assert glint.rest_minutes(now=1000.0, path=state) == 0.0


def test_rest_clock_accumulates_across_renders():
    with tempfile.TemporaryDirectory() as d:
        state = str(Path(d) / "rest.json")
        _rest_state(state, start=0.0, last=60.0)          # 1 min in, seen 1 min ago
        assert glint.rest_minutes(now=120.0, path=state) == 2.0


def test_rest_clock_resets_after_a_real_break():
    with tempfile.TemporaryDirectory() as d:
        state = str(Path(d) / "rest.json")
        # Worked an hour, then no render for 11 minutes: that was a break.
        _rest_state(state, start=0.0, last=3600.0)
        assert glint.rest_minutes(now=3600.0 + 11 * 60, path=state) == 0.0


def test_rest_clock_survives_a_gap_shorter_than_the_break():
    with tempfile.TemporaryDirectory() as d:
        state = str(Path(d) / "rest.json")
        _rest_state(state, start=0.0, last=3600.0)
        mins = glint.rest_minutes(now=3600.0 + 9 * 60, path=state)   # 9 min < 10
        assert round(mins) == 69


def test_rest_clock_ignores_a_corrupt_or_future_state_file():
    with tempfile.TemporaryDirectory() as d:
        state = str(Path(d) / "rest.json")
        with open(state, "w") as f:
            f.write("{not json")
        assert glint.rest_minutes(now=500.0, path=state) == 0.0
        _rest_state(state, start=9e9, last=500.0)          # start in the future
        assert glint.rest_minutes(now=500.0, path=state) == 0.0


def test_rest_segment_thresholds():
    assert glint.rest_segment(0) == ""
    assert glint.rest_segment(29) == ""                     # below REST_SHOW
    assert "🪑" in glint.rest_segment(34)                   # quiet clock
    assert "☕" in glint.rest_segment(52)                    # nudge
    assert "🛑" in glint.rest_segment(95)                    # ultradian cycle
    assert "34m" in glint.rest_segment(34)
    assert "1h35m" in glint.rest_segment(95)


def test_rest_segment_says_what_to_do():
    # An emoji and a number alone don't tell you which clock this is.
    assert "break" in glint.rest_segment(52)
    assert "stand up" in glint.rest_segment(95)
    assert "break" not in glint.rest_segment(34)            # nothing to do yet


def test_rest_priority_escalates_with_fatigue():
    assert glint.rest_priority(34) == glint.PRIO_REST_QUIET
    assert glint.rest_priority(52) == glint.PRIO_REST_NUDGE
    assert glint.rest_priority(95) == glint.PRIO_REST_HARD
    # ordering is what matters: later nudges survive a narrower line
    assert glint.PRIO_REST_HARD < glint.PRIO_REST_NUDGE < glint.PRIO_REST_QUIET
    assert glint.PRIO_REST_HARD > glint.PRIO_MODEL          # never beats identity


def test_stand_up_nudge_outlives_the_cost_segment():
    line = glint._ANSI.sub("", glint.fit([
        (glint.PRIO_MODEL, glint.GRP_SESSION, "model"),
        (glint.PRIO_COST, glint.GRP_CHANGE, "$1.20"),
        (glint.rest_priority(95), glint.GRP_REST, "stand up"),
    ], 20))
    assert "stand up" in line and "$1.20" not in line


def test_rest_thresholds_follow_the_nudge_env():
    os.environ["GLINT_REST_NUDGE"] = "25"
    try:
        assert "☕" in glint.rest_segment(26)                # nudge moved down
        assert "🪑" in glint.rest_segment(16)                # show = 60% of nudge
        assert glint.rest_segment(14) == ""
        assert "🛑" in glint.rest_segment(46)                # hard = 1.8x nudge
    finally:
        del os.environ["GLINT_REST_NUDGE"]


def test_rest_env_minutes_rejects_junk():
    try:
        for bad in ("", "abc", "0", "-5"):
            os.environ["GLINT_REST_NUDGE"] = bad
            assert glint.env_minutes("REST_NUDGE", 50.0) == 50.0, bad
    finally:
        os.environ.pop("GLINT_REST_NUDGE", None)


def test_vis_width_counts_text_glyphs_as_one_cell():
    # ✻ (U+273B) and ⚠ (U+26A0) sit in a block full of emoji but render narrow;
    # counting them as two made fit() drop segments the terminal had room for.
    assert glint.vis_width("✻ S5") == 4
    assert glint.vis_width("⚠ 12k") == 5
    assert glint.vis_width("+1.2k/-340") == 10


def test_vis_width_counts_emoji_as_two_cells():
    assert glint.vis_width("📁 glint") == 8          # 2 + 1 + 5
    assert glint.vis_width("☕ 52m") == 6            # emoji-by-default
    assert glint.vis_width("♻️ 94%") == 6            # widened by U+FE0F
    assert glint.vis_width("♻ 94%") == 5             # same glyph, no selector


def test_vis_width_ignores_colour_and_hyperlinks():
    plain = glint.vis_width("#42")
    assert glint.vis_width(glint.c("#42", glint.BLUE)) == plain
    assert glint.vis_width(glint.link("#42", "https://example.com/very/long/url")) == plain


def test_only_https_urls_become_hyperlinks():
    assert glint._safe_url("https://github.com/o/r/pull/1") == "https://github.com/o/r/pull/1"
    for hostile in ("file:///etc/passwd", "javascript:alert(1)", "http://x",
                    "https://x\nmore", None, 42):
        assert glint._safe_url(hostile) == "", hostile


def test_cache_files_are_private_and_ownership_checked():
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "cache.json")
        glint._write_private(path, {"at": 1, "pr": None})
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"
        assert glint._load_own_json(path) == {"at": 1, "pr": None}
        # a symlink is not a plain file we own
        link_path = str(Path(d) / "linked.json")
        os.symlink(path, link_path)
        try:
            glint._load_own_json(link_path)
            raised = False
        except OSError:
            raised = True
        assert raised


def test_rest_status_does_not_count_as_activity():
    with tempfile.TemporaryDirectory() as d:
        state = str(Path(d) / "rest.json")
        now = time.time()
        _rest_state(state, start=now - 3000, last=now - 540)      # idle 9 min
        glint.rest_minutes(now=now, path=state, write=False)
        with open(state) as f:
            assert round(now - json.load(f)["last"]) == 540       # untouched


def test_unknown_flag_exits_instead_of_waiting_on_stdin():
    p = subprocess.run([sys.executable, str(HERE / "glint.py"), "--rest"],
                       capture_output=True, text=True, timeout=10)
    assert p.returncode == 2
    assert "unknown option" in p.stderr


def test_version_flag_matches_the_changelog():
    p = subprocess.run([sys.executable, str(HERE / "glint.py"), "--version"],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == f"glint {glint.__version__}"
    # the changelog's newest entry is the release this file claims to be
    changelog = (HERE / "CHANGELOG.md").read_text()
    assert f"## [{glint.__version__}]" in changelog


def test_rested_flag_zeroes_the_clock():
    with tempfile.TemporaryDirectory() as d:
        state = str(Path(d) / "rest.json")
        now = time.time()
        _rest_state(state, start=now - 70 * 60, last=now)
        env = {k: v for k, v in os.environ.items() if not k.startswith("GLINT_")}
        env["GLINT_REST_STATE"] = state
        p = subprocess.run([sys.executable, str(HERE / "glint.py"), "--rested"],
                           capture_output=True, text=True, env=env)
        assert p.returncode == 0, p.stderr
        assert "break logged" in p.stdout
        with open(state) as f:
            after = json.load(f)
        assert after["last"] - after["start"] < 1        # back to zero


def test_rest_status_reports_without_resetting():
    with tempfile.TemporaryDirectory() as d:
        state = str(Path(d) / "rest.json")
        now = time.time()
        _rest_state(state, start=now - 42 * 60, last=now)
        env = {k: v for k, v in os.environ.items() if not k.startswith("GLINT_")}
        env["GLINT_REST_STATE"] = state
        p = subprocess.run([sys.executable, str(HERE / "glint.py"), "--rest-status"],
                           capture_output=True, text=True, env=env)
        assert p.returncode == 0, p.stderr
        assert "42 min" in p.stdout
        with open(state) as f:
            after = json.load(f)
        assert after["last"] - after["start"] > 40 * 60      # clock still running


def test_rest_reset_reports_failure_instead_of_lying():
    assert glint.rest_reset(path="/nope/does/not/exist/rest.json") is False


def test_rest_segment_renders_and_can_be_switched_off():
    payload = {"model": {"display_name": "Sonnet 5"}, "cwd": "/tmp"}
    with tempfile.TemporaryDirectory() as d:
        state = str(Path(d) / "rest.json")
        now = time.time()
        _rest_state(state, start=now - 3600, last=now)        # an hour of work

        on = render(payload, GLINT_REST="1", GLINT_REST_STATE=state)
        assert "☕" in on and "1h00m" in on and "break" in on

        off = render(payload, GLINT_REST="0", GLINT_REST_STATE=state)
        assert "☕" not in off


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
