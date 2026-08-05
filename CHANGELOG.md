# Changelog

All notable changes to `glint`, newest first. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[semantic versioning](https://semver.org/spec/v2.0.0.html).

Upgrading is always the same one-liner. It overwrites `~/.claude/glint.py` and
rewrites `statusLine` plus the `GLINT_*` keys in `settings.json`, keeping the
preferences you already chose and every unrelated setting untouched:

```bash
curl -fsSL https://raw.githubusercontent.com/oleg-koval/glint/main/install.sh | bash
```

## [1.2.0] — 2026-08-05

### Added

- **Compaction alert** (`glint_alert.py`) — an optional companion [Stop
  hook](https://docs.claude.com/en/docs/claude-code/hooks). The status line
  *shows* context filling up; this *tells* you, with a notification and sound at
  75% and again at 90%, plus an inline reminder of exactly what to run and where
  to type it. Debounced per session per tier, so it never nags turn-to-turn.
  Same rules as the status line: zero dependencies, and any failure just means
  no alert. `GLINT_ALERT_SILENT=1` keeps the inline note without the OS
  notification. Setup is in the README.

### Fixed

- **glint now works on Windows.** `os.getuid` is Unix-only, so every temp-cache
  path raised there and the whole line collapsed to the bare `✻ Claude`
  fallback — it worked everywhere except the platform nobody had tested. Paths
  now fall back to the username, and the cache ownership check is skipped where
  the OS reports no meaningful owner.

## [1.1.0] — 2026-08-05

Everything since the first release: seven new segments, a layout that groups
them, and the first thing on the line that isn't about the machine.

### Added

- **Break reminder.** A new group tracks how long you've worked without
  stopping, and stays invisible until 30 minutes have passed: dim `🪑 34m`,
  then `☕ 52m break` at 50 minutes, then a bold red `🛑 1h35m stand up` at 90.
  Claude Code renders on activity, so the gap between renders is idle time —
  ten quiet minutes counts as a break and restarts the clock. Took a shorter
  break? `glint.py --rested` logs it; `glint.py --rest-status` prints the clock.
  Tune with `GLINT_REST_NUDGE` (moves the whole ladder) or switch it off with
  `GLINT_REST=0`.
- **Topic groups.** Segments are grouped `session ┃ place ┃ change ┃ budget ┃
  rest` and divided by a dim bar, so your eye lands on the group it wants
  instead of scanning ten segments in one uniform row.
- **Open pull request** for the current branch — number, draft state and CI
  rollup, as an OSC 8 hyperlink you can click. Looked up in a detached
  background `gh` call and cached, so it never delays a render.
- **Prompt-cache hit ratio** (`♻️ 94%`) — the share of your context served from
  cache, where reads are ~10× cheaper than fresh input.
- **Quota pace marker** (`⚡110%`) — appears only when you're burning quota
  faster than the window elapses; the share you'd need by reset at this rate.
- **Rate-limit windows** (`⏱5h 63% ↻1h07m`, `📅7d 10%`) with reset ETAs, read
  from the payload when your plan reports them.
- **Runway countdown** on the context segment: `→ 50k left` from 70% full, a
  bold red `⚠ 12k left` from 85%.
- **Worktree segment** (`🌳 wt-refactor`), shown only when it differs from the
  directory segment, so ordinary repos stay quiet.
- **Effort badge and fast mode** (`H`, `⏩`), hidden on models that don't
  report effort rather than assuming a default.
- **Per-segment toggles**: `GLINT_COST`, `GLINT_LINES`, `GLINT_CACHE`,
  `GLINT_RATELIMITS`, `GLINT_WORKTREE`, `GLINT_PR`, `GLINT_REST`.
- **Width-aware layout.** Segments carry a priority and the line drops the
  least important ones until it fits, putting back anything that still has
  room; the model badge is never dropped. Emoji are measured as two cells, and
  hyperlink escapes as zero, so the fit is honest.

### Changed

- **The `▕███░░▏` gauges are now opt-in** — set `GLINT_BARS=1`, or answer the
  question the installer asks. It's the only thing this release *removes* from
  the default line: the coloured percentage already carried the signal, and the
  blocks cost ~10 columns that a narrow terminal would rather spend on a
  segment. (Topic groups and the break reminder change the line too, but by
  rearranging and adding.)
- Model badges are abbreviated to family + version (`Sonnet 4.6` → `S4.6`).
- The context window size reported by Claude Code is trusted when present, so a
  1M session is never drawn against a 200k limit.
- `install.sh` takes `--bars` / `--no-bars` / `--rest` / `--no-rest` /
  `--rest-nudge N`, and writes your answers under `env` in `settings.json`
  without touching unrelated keys.

### Fixed

- Every line was measured too wide: `✻` in the model badge and `⚠` in the
  context warning were counted as two cells each, so segments were dropped on
  terminals that had room for them. Width now follows emoji presentation — a
  glyph is wide if it's emoji-by-default or carries a `U+FE0F` selector.
- `--rest-status` used to stamp the clock while reading it, which restarted
  idle-gap detection: checking the status and then stepping away for nine
  minutes lost the break. Reading is now read-only.
- An unrecognised flag fell through to reading stdin and hung with no output
  until `Ctrl-D`. It now prints usage and exits 2.
- The PR cache and rest clock are written `0600` with `O_EXCL`, read back only
  if we own them and they're plain files, and an open-PR URL is only turned into
  a hyperlink when it's `https://` — a predictable name in a shared temp dir
  shouldn't let another local user choose where an invisible link points.
- Re-running the installer (i.e. upgrading) reset your stored preferences: a
  recorded `GLINT_BARS=1` or `GLINT_REST=0` was dropped when the new run didn't
  mention it. Stored settings are now the starting point, with flags and
  exported variables taking precedence, and truthy spellings (`true`, `yes`,
  `on`) are accepted everywhere.
- The installer printed a prompt and then an error where `/dev/tty` exists but
  can't be opened (containers, CI). It now checks by opening it, and stays
  silent with no terminal attached.
- The test suite inherited `GLINT_*` from your shell, so an exported
  `GLINT_COST=0` failed an unrelated test. Tests now scrub those variables and
  keep the rest clock in a throwaway file, never your real one.

## [1.0.0] — 2026-06-20

First public release: model, directory, git branch with dirty count and
ahead/behind, session cost and duration, lines changed, and the live
context-window gauge. One file, zero dependencies, graceful degradation, and a
bare `✻ Claude` fallback so the prompt never breaks.

[1.2.0]: https://github.com/oleg-koval/glint/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/oleg-koval/glint/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/oleg-koval/glint/releases/tag/v1.0.0
