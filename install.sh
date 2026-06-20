#!/usr/bin/env bash
# glint installer — drops glint.py into ~/.claude and wires it as the Claude Code
# status line. Idempotent: re-running just refreshes the script and the setting.
#
#   curl -fsSL https://raw.githubusercontent.com/oleg-koval/glint/main/install.sh | bash
#
set -euo pipefail

RAW="https://raw.githubusercontent.com/oleg-koval/glint/main/glint.py"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
DEST="$CLAUDE_DIR/glint.py"
SETTINGS="$CLAUDE_DIR/settings.json"

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

echo "→ wiring status line in $SETTINGS"
python3 - "$DEST" "$SETTINGS" <<'PY'
import json, os, sys
dest, settings = sys.argv[1], sys.argv[2]
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
with open(settings, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
PY

echo "✓ glint installed. Restart Claude Code (or open a new session) to see it."
