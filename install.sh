#!/usr/bin/env bash
# glint installer — drops glint.py into ~/.claude and wires it as the Claude Code
# status line. Idempotent: re-running just refreshes the script and the setting.
#
#   curl -fsSL https://raw.githubusercontent.com/oleg-koval/glint/main/install.sh | bash
#
# Gauges (▕███░░▏ next to the context and rate-limit percentages) are off by
# default; the installer asks, and you can decide up front instead:
#
#   ... | bash -s -- --bars       # or GLINT_BARS=1 before the pipe
#   ... | bash -s -- --no-bars    # never ask
#
# The break reminder is on (it stays hidden until 30 unbroken minutes of work):
#
#   ... | bash -s -- --no-rest         # don't remind me
#   ... | bash -s -- --rest-nudge 40   # nudge after 40 min instead of 50
#
set -euo pipefail

RAW="https://raw.githubusercontent.com/oleg-koval/glint/main/glint.py"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
DEST="$CLAUDE_DIR/glint.py"
SETTINGS="$CLAUDE_DIR/settings.json"

# Preferences already in settings.json are the starting point, so upgrading
# ("curl | bash" again, with no flags) keeps what you chose last time instead of
# resetting it. Precedence: flags > exported env > stored > default.
read_stored() {
  [ -f "$SETTINGS" ] || return 0
  python3 -c '
import json, sys
try:
    with open(sys.argv[1]) as f:
        env = (json.load(f) or {}).get("env") or {}
except Exception:
    env = {}
def truthy(v):
    return "1" if str(v).strip().lower() in ("1", "true", "yes", "on") else "0"
for key, name in (("GLINT_BARS", "STORED_BARS"), ("GLINT_REST", "STORED_REST")):
    if key in env:
        print(name + "=" + truthy(env[key]))
nudge = str(env.get("GLINT_REST_NUDGE", "")).strip()
if nudge.isdigit():
    print("STORED_NUDGE=" + nudge)
' "$SETTINGS"
}
STORED_BARS=""; STORED_REST=""; STORED_NUDGE=""
eval "$(read_stored)"

# unset = ask; 1/0 = decided. An exported GLINT_BARS counts as a decision, and
# so does one already recorded in settings.json.
BARS="${GLINT_BARS:-$STORED_BARS}"
REST="${GLINT_REST:-${STORED_REST:-1}}"
REST_NUDGE="${GLINT_REST_NUDGE:-$STORED_NUDGE}"

# Accept the spellings glint itself accepts, so GLINT_BARS=true doesn't read as
# "decided: off" further down.
lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }
case "$(lower "$BARS")" in
  1|true|yes|on) BARS=1 ;;
  "") BARS="" ;;
  *) BARS=0 ;;
esac
case "$(lower "$REST")" in
  0|false|no|off) REST=0 ;;
  *) REST=1 ;;
esac
while [ $# -gt 0 ]; do
  case "$1" in
    --bars) BARS=1 ;;
    --no-bars) BARS=0 ;;
    --rest) REST=1 ;;
    --no-rest) REST=0 ;;
    --rest-nudge) shift; REST_NUDGE="${1:-}"
      case "$REST_NUDGE" in ''|*[!0-9]*) echo "--rest-nudge wants whole minutes" >&2; exit 2 ;; esac ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

command -v python3 >/dev/null 2>&1 || { echo "glint needs python3 (not found). Install it and re-run."; exit 1; }

mkdir -p "$CLAUDE_DIR"

echo "→ fetching glint.py"
if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$RAW" -o "$DEST"
elif command -v wget >/dev/null 2>&1; then
  wget -qO "$DEST" "$RAW"
else
  echo "need curl or wget"; exit 1
fi
chmod +x "$DEST"

# Ask about gauges. Piped through curl, stdin is the script itself, so read the
# keyboard from /dev/tty. Open it before printing anything: the file can exist
# and still not be connected (containers, CI, provisioning), and a prompt nobody
# can answer is worse than the quiet default.
if [ -z "$BARS" ]; then
  BARS=0
  # The brace group's redirect is applied before the failing one inside it, so a
  # missing tty stays silent; fd 3 outlives the group because it runs in-shell.
  if { exec 3</dev/tty; } 2>/dev/null; then
    printf '\n  Percentages are always coloured. Add gauges too?\n'
    printf '    off  🧠 36%% 357k/1.0M   ⏱5h 63%%\n'
    printf '    on   🧠 36%% ▕███░░░░░▏ 357k/1.0M   ⏱5h 63%%▕███░░▏\n'
    printf '  Show gauges? [y/N] '
    if read -r reply <&3; then
      case "$reply" in [yY]*) BARS=1 ;; esac
    else
      printf '\n'
    fi
    exec 3<&-
  fi
fi

echo "→ wiring status line in $SETTINGS"
python3 - "$DEST" "$SETTINGS" "$BARS" "$REST" "$REST_NUDGE" <<'PY'
import json, os, sys
dest, settings, bars = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
rest, rest_nudge = sys.argv[4] == "1", sys.argv[5]
cfg = {}
if os.path.exists(settings):
    try:
        with open(settings) as f:
            cfg = json.load(f)
    except Exception:
        # Don't clobber an unreadable settings file — back it up first.
        os.replace(settings, settings + ".bak")
        print(f"  (existing settings.json was invalid; backed up to {settings}.bak)")
cfg["statusLine"] = {"type": "command", "command": f'python3 "{dest}"'}

# Gauges are opt-in, so "no" means drop the key rather than pin it to 0 — that
# way a re-run with --bars, or an exported GLINT_BARS, still takes effect.
env = cfg.get("env")
if not isinstance(env, dict):
    env = {}
if bars:
    env["GLINT_BARS"] = "1"
else:
    env.pop("GLINT_BARS", None)

# The reminder ships on, so here it's the "off" that needs recording.
if rest:
    env.pop("GLINT_REST", None)
else:
    env["GLINT_REST"] = "0"
if rest_nudge:
    env["GLINT_REST_NUDGE"] = rest_nudge
if env:
    cfg["env"] = env
elif "env" in cfg:
    del cfg["env"]

with open(settings, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
print("  gauges: " + ("on (GLINT_BARS=1)" if bars else "off — re-run with --bars to enable"))
print("  break reminder: " + (f"after {env.get('GLINT_REST_NUDGE', 50)} min of unbroken work"
                             if rest else "off"))
PY

eval "$(read_stored)"
REST_NUDGE="${STORED_NUDGE:-50}"

echo "✓ glint installed. Restart Claude Code (or open a new session) to see it."

if [ "$REST" = "1" ]; then
  cat <<EOF

  Break reminder: 🪑 clock at 30 min · ☕ break at ${REST_NUDGE} · 🛑 stand up at 1.8x that.
  Walking away for 10 min resets it. Took a shorter break? Tell it so:

    python3 "$DEST" --rested        # clock back to zero
    python3 "$DEST" --rest-status   # how long have I been at it?

  Worth an alias:  alias rested='python3 "$DEST" --rested'
EOF
fi
