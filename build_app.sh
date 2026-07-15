#!/usr/bin/env bash
# build_app.sh — build the VoiceDictation.app menu bar bundle (py2app alias mode).
#
# Alias mode gives the app its own identity (so macOS attaches Microphone /
# Accessibility / Input Monitoring permissions to VoiceDictation.app itself, and
# they persist across relaunches) while symlinking back to this project's venv.
# The .app therefore depends on the venv staying at its current path.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

if [ ! -d venv ]; then
  echo "venv not found. Run ./install.sh first." >&2
  exit 1
fi
# shellcheck disable=SC1091
source venv/bin/activate

echo "==> Building VoiceDictation.app (alias mode)…"
rm -rf build dist
python setup.py py2app -A 2>&1 | tail -5

APP="$HERE/dist/VoiceDictation.app"

# Re-sign ad-hoc so macOS/TCC can validate the bundle and list it in the
# Privacy panes (alias mode's symlinked python otherwise trips --deep verify).
echo "==> Re-signing bundle (ad-hoc)…"
codesign --force --sign - "$APP" >/dev/null 2>&1 || true
codesign --verify --verbose=2 "$APP" 2>&1 | tail -2

# Register with Launch Services so it appears to the system.
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" || true

echo ""
echo "==> Built: $APP"
echo "    Launch it with:  open dist/VoiceDictation.app"
echo "    Menu bar 🎙️ icon will appear (no Dock icon — it's a menu bar agent)."
