#!/usr/bin/env bash
# glint for Codex CLI. Codex has no status-line hook (its own bar takes a fixed
# list of built-in items), so glint reads the session log Codex already writes
# and renders into your tmux status bar instead.
#
#   curl -fsSL https://raw.githubusercontent.com/oleg-koval/glint/main/install-codex.sh | bash
#
# Flags: --print (show the tmux lines, change nothing) and the same
# --bars / --no-rest / --rest-nudge N the Claude Code installer takes.
#
set -euo pipefail

RAW="https://raw.githubusercontent.com/oleg-koval/glint/main/glint.py"
DEST_DIR="${GLINT_DIR:-$HOME/.claude}"
DEST="$DEST_DIR/glint.py"
TMUX_CONF="${TMUX_CONF:-$HOME/.tmux.conf}"
PRINT_ONLY=0
BARS="${GLINT_BARS:-}"
REST_NUDGE="${GLINT_REST_NUDGE:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --print) PRINT_ONLY=1 ;;
    --bars) BARS=1 ;;
    --no-bars) BARS=0 ;;
    --no-rest) export GLINT_REST=0 ;;
    --rest-nudge) shift; REST_NUDGE="${1:-}"
      case "$REST_NUDGE" in ''|*[!0-9]*) echo "--rest-nudge wants whole minutes" >&2; exit 2 ;; esac ;;
    -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

command -v python3 >/dev/null 2>&1 || { echo "glint needs python3 (not found)."; exit 1; }

if [ ! -d "${CODEX_HOME:-$HOME/.codex}" ]; then
  echo "note: ${CODEX_HOME:-$HOME/.codex} not found. Installing anyway; the context and"
  echo "      quota segments stay hidden until Codex has written a session."
fi

# --print is a dry run: it must not fetch, install, or overwrite anything, so
# preview with whatever glint is already here (repo checkout first, then the
# installed copy). Fetching under --print once clobbered a working copy.
if [ "$PRINT_ONLY" = "1" ]; then
  for cand in "./glint.py" "$DEST"; do
    [ -f "$cand" ] && PREVIEW="$cand" && break
  done
fi

if [ "$PRINT_ONLY" != "1" ]; then
mkdir -p "$DEST_DIR"
echo "-> fetching glint.py"
if command -v curl >/dev/null 2>&1; then curl -fsSL "$RAW" -o "$DEST"
elif command -v wget >/dev/null 2>&1; then wget -qO "$DEST" "$RAW"
else echo "need curl or wget"; exit 1; fi
chmod +x "$DEST"
fi

# Single quotes inside: the tmux value is already double-quoted, and a nested
# double quote would end the string mid-path.
DEST_ESCAPED="${DEST//\'/\'\\\'\'}"
CMD="python3 '$DEST_ESCAPED' --harness codex --tmux"
[ -n "$REST_NUDGE" ] && CMD="GLINT_REST_NUDGE=$REST_NUDGE $CMD"
[ "${BARS:-0}" = "1" ] && CMD="GLINT_BARS=1 $CMD"
[ "${GLINT_REST:-}" = "0" ] && CMD="GLINT_REST=0 $CMD"

BLOCK=$(cat <<EOF
# ── glint (Codex) ── managed block, safe to move but keep the markers
set -g status-right-length 200
set -g status-right "#($CMD)"
set -g status-interval 5
# ── end glint ──
EOF
)

if [ "$PRINT_ONLY" = "1" ]; then
  printf '%s\n' "$BLOCK"
  echo
  if [ -n "${PREVIEW:-}" ]; then
    echo "Preview from $PREVIEW:"
    python3 "$PREVIEW" --harness codex --width 160 || true
  else
    echo "(no local glint.py to preview with; re-run without --print to install)"
  fi
  echo
  exit 0
fi

echo "-> writing the tmux block in $TMUX_CONF"
python3 - "$TMUX_CONF" "$BLOCK" <<'PY'
import sys
path, block = sys.argv[1], sys.argv[2]
start, end = "# ── glint (Codex) ──", "# ── end glint ──"
try:
    with open(path) as f:
        conf = f.read()
except FileNotFoundError:
    conf = ""
if start in conf and end in conf:
    head, rest = conf.split(start, 1)
    _old, tail = rest.split(end, 1)
    conf = head + block.rstrip("\n") + tail          # replace, never duplicate
    print("  (refreshed the existing glint block)")
else:
    conf = conf.rstrip("\n") + ("\n\n" if conf.strip() else "") + block.rstrip("\n") + "\n"
with open(path, "w") as f:
    f.write(conf)
PY

echo "-> checking it renders"
python3 "$DEST" --harness codex --width 160
echo

if [ -n "${TMUX:-}" ]; then
  tmux source-file "$TMUX_CONF" >/dev/null 2>&1 && echo "✓ reloaded tmux config" \
    || echo "! could not reload; run: tmux source-file $TMUX_CONF"
else
  echo "✓ installed. Start tmux (or run: tmux source-file $TMUX_CONF) to see it."
fi
echo
echo "  Codex's own bar stays as it is; this adds glint to the tmux status line."
echo "  Break and hydration reminders work here too:"
echo "    python3 \"$DEST\" --rested     # took a break"
echo "    python3 \"$DEST\" --drank      # drank at my desk"
