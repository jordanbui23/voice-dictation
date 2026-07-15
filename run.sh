#!/usr/bin/env bash
# run.sh — launch the voice-dictation menu bar app from its venv.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
if [ ! -d venv ]; then
  echo "venv not found. Run ./install.sh first." >&2
  exit 1
fi
# shellcheck disable=SC1091
source venv/bin/activate
exec python src/app.py
