#!/usr/bin/env bash
# install.sh — one-shot setup for voice-dictation.
# - installs python@3.12 via brew (3.14 lacks reliable wheels for these deps)
# - builds venv, installs deps
# - downloads the default Whisper model
# - runs a Bedrock connectivity check against the configured AWS profile
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

echo "==> voice-dictation install"

# --- 1. Python 3.12 ---
if ! command -v brew >/dev/null 2>&1; then
  echo "ERROR: Homebrew not found. Install from https://brew.sh first." >&2
  exit 1
fi

if ! brew list python@3.12 >/dev/null 2>&1; then
  echo "==> Installing python@3.12 via brew…"
  brew install python@3.12
fi

PY312="$(brew --prefix python@3.12)/bin/python3.12"
if [ ! -x "$PY312" ]; then
  PY312="$(command -v python3.12 || true)"
fi
if [ -z "$PY312" ] || [ ! -x "$PY312" ]; then
  echo "ERROR: python3.12 not found after install." >&2
  exit 1
fi
echo "==> Using interpreter: $PY312 ($($PY312 --version))"

# --- 2. venv ---
if [ ! -d venv ]; then
  echo "==> Creating venv…"
  "$PY312" -m venv venv
fi
# shellcheck disable=SC1091
source venv/bin/activate
python -m pip install --upgrade pip wheel >/dev/null

# --- 3. deps ---
echo "==> Installing dependencies (this pulls mlx-whisper, may take a few minutes)…"
pip install -r requirements.txt

# --- 4. download default Whisper model ---
echo "==> Warming up default Whisper model (downloads on first run)…"
python - <<'PYEOF'
import sys, os
sys.path.insert(0, os.path.join(os.getcwd(), "src"))
from config import Config
from transcribe import Transcriber
cfg = Config()
print(f"    model: {cfg.get('whisper_model')}")
Transcriber(cfg.get("whisper_model")).warm_up()
print("    Whisper model ready.")
PYEOF

# --- 5. Bedrock connectivity check ---
echo "==> Checking Bedrock connectivity (configured profile, Haiku)…"
python - <<'PYEOF'
import sys, os
sys.path.insert(0, os.path.join(os.getcwd(), "src"))
from config import Config
from bedrock_cleanup import smoke_test
cfg = Config()
try:
    out = smoke_test(cfg.get("aws_profile"), cfg.get("aws_region"), cfg.get("bedrock_model_id"))
    print(f"    Bedrock OK. Sample cleaned output:\n    {out!r}")
except Exception as e:
    print(f"    WARNING: Bedrock check failed: {e}")
    print("    The app will still run and paste raw transcripts (cleanup will fall back).")
PYEOF

# --- 6. build the .app bundle ---
echo "==> Building VoiceDictation.app menu bar bundle…"
./build_app.sh

echo ""
echo "==> Install complete."
echo ""
echo "    Two ways to run:"
echo "      • Menu bar app (recommended):  open dist/VoiceDictation.app"
echo "        — its own identity; permissions attach to VoiceDictation.app and persist."
echo "      • Dev/script mode:             ./run.sh"
echo "        — permissions attach to the launching terminal instead."
echo ""
echo "    First launch needs three permissions (System Settings ▸ Privacy & Security):"
echo "      1. Input Monitoring  — global hold-to-talk hotkey"
echo "      2. Microphone        — prompts automatically on your first recording"
echo "      3. Accessibility     — paste at cursor (add via + if not auto-listed)"
