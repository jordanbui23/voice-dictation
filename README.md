# Voice Dictation

A menu bar dictation app for Apple Silicon Macs. Hold a hotkey, speak, release —
text appears at your cursor in whatever app is focused (Slack, terminal, OpenCode,
browser). Runs two ways depending on the cleanup toggle: a polished **batch** mode
(local transcription + Bedrock cleanup, pasted on release) and a fully-offline
**live streaming** mode (words typed at the cursor as you speak).

## Architecture

```
  BATCH mode (cleanup ON, default):
    your voice ──▶ [LOCAL Whisper]  ──▶ raw text
               ──▶ [AWS Bedrock · Claude Haiku]  ──▶ polished text
               ──▶ pasted at cursor (clipboard saved & restored)

  STREAMING mode (cleanup OFF, fully offline):
    your voice ──▶ [LOCAL Whisper, chunk-and-finalize] ──▶ words typed live at cursor
               (no clipboard, no network — audio and text never leave the Mac)
```

- **Stage 1 (transcription) runs 100% locally** via `mlx-whisper` (Metal). **Audio
  never leaves the machine**, in either mode.
- **Stage 2 (Bedrock cleanup)** sends **only the raw transcript text** (never audio)
  to Bedrock for grammar/punctuation fixes and technical-term correction. Toggleable.
- **The cleanup toggle also selects the mode:**
  - **Cleanup ON → batch:** transcribe the whole clip on release, clean via Bedrock,
    paste at cursor with the clipboard preserved. Default.
  - **Cleanup OFF → streaming:** live sliding-window transcription types stable words
    at the cursor via synthesized keystrokes as you speak. Fully offline, zero network.
- If the Bedrock call fails/times out (>3s), it **falls back to the raw transcript**.
  On an auth failure (expired credentials), a modal alert tells you to refresh your
  AWS credentials. Your words are never lost.

### Streaming internals (chunk-and-finalize)

Whisper is a batch model, so streaming re-transcribes a **bounded recent window** of
audio on an interval and permanently commits words once they have aged past a
volatility margin (Whisper revises the last ~1–2s of audio). Committed audio is never
re-decoded, so decode time stays flat regardless of how long you talk (long journaling
does not get laggy), and typed text is append-only — never rewritten or backspaced.
Keystrokes are posted to the session event tap on the main thread with modifier flags
cleared, so a held modifier hotkey never turns typed text into shortcuts.

## AWS / Bedrock

Stage 2 cleanup calls Amazon Bedrock. Configure your own AWS profile, region, and
model in `config.json` (defaults live in `src/config.py`):

- Profile: any local AWS profile with Bedrock `InvokeModel` access (default `default`),
  region `us-west-2`.
- Model: `us.anthropic.claude-haiku-4-5-20251001-v1:0` (a cross-region inference
  profile). If your account's role only permits `InvokeModel` via **inference
  profiles**, use the `us.` profile ID rather than a bare foundation-model ID.

## Install

```bash
cd ~/projects/voice-dictation
./install.sh
```

`install.sh` installs `python@3.12` via Homebrew (the app's deps lack reliable wheels
for the system's Python 3.14), builds a venv, installs dependencies, downloads the
default Whisper model, runs a Bedrock connectivity check, and builds the menu bar
app bundle.

## Run

Two ways:

```bash
open dist/VoiceDictation.app   # menu bar app (recommended)
./run.sh                       # dev/script mode
```

Prefer **`open dist/VoiceDictation.app`**: the bundle has its own identity, so macOS
attaches permissions to `VoiceDictation.app` itself and they persist across relaunches.
`./run.sh` instead attaches permissions to whatever terminal launched it (handy for
iterating on the code, but the perms are tied to the terminal).

A 🎙️ icon appears in the menu bar (no Dock icon — it's a menu bar agent).
**Hold Right Command + Right Option, speak, release.**

While recording, a floating **notch overlay pill** appears top-center under the
notch and cycles `🔴 Listening…` → `⏳ Transcribing…` → `✓ Done` (then fades). This
is the primary status indicator — the menu bar `🎙️` icon stays put.

To rebuild the bundle after editing the code: `./build_app.sh`.

### First-run permissions

Grant all three in **System Settings → Privacy & Security** (to `VoiceDictation` when
launched via the bundle, or to your terminal when using `./run.sh`):

1. **Input Monitoring** — the global hold-to-talk hotkey (CGEventTap). Grant this first.
2. **Microphone** — capture audio. This one does **not** appear in the list until the
   app first tries to record — macOS shows an "allow microphone" prompt on your first
   hold-to-talk. Click **Allow**, then dictate again.
3. **Accessibility** — type/paste at the cursor. If `VoiceDictation` isn't
   auto-listed, click **+** and add `dist/VoiceDictation.app`.

Toggling Accessibility or Input Monitoring may prompt "Quit & Reopen" — allow it. After
granting, relaunch the app.

## Settings (menu bar dropdown)

- **Cleanup (Bedrock)** — toggle Stage 2 on/off. **This also selects the mode:** ON =
  batch + Bedrock polish (paste on release); OFF = live offline streaming (typed as you
  speak).
- **Whisper Model** — `tiny` / `base` / `small` / `medium`. Bigger = more accurate,
  slower. Default `base`.
- **Hotkey** — Right Command + Right Option (default), or single Right Command / Right
  Option / Right Control, or F5. A non-modifier key (F5) or the modifier-clearing guard
  keeps held modifiers from corrupting typed text in streaming mode.
- **Open Log Folder** — transcripts logged to `logs/transcripts.jsonl`.

Settings persist to `config.json` at the project root. Notable extra keys:
`stream_interval_seconds` (streaming decode cadence, default 0.5), `stream_debug`
(when true, writes streaming diagnostics to `logs/stream_debug.log`; default false).

## Logs

Every dictation is appended to `logs/transcripts.jsonl` (one JSON object per line:
timestamp, raw transcript, final text, whether cleanup was used, any cleanup error).

## Auto-start at login

`VoiceDictation.app` is registered as a macOS **Login Item**, so it launches
automatically on login/restart — no need to start it manually. Because it's a proper
`.app` bundle with its own identity, the Microphone / Accessibility / Input Monitoring
grants persist across restarts. Manage it in **System Settings → General → Login Items**.

Caveat: the app is a py2app **alias-mode** bundle — it symlinks back to this project's
`venv` and `src/`. Don't move or delete `~/projects/voice-dictation/` or the app breaks.
Rebuilding in place (`./build_app.sh`) keeps the same path, so the login item still works.

## Files

| File | Purpose |
|------|---------|
| `src/app.py` | Menu bar app; wires the whole pipeline, mode selection, overlay, auth modal |
| `src/hotkey.py` | Global hold-to-talk via CGEventTap (pyobjc, not pynput); supports modifier combos |
| `src/audio.py` | 16 kHz mono capture (sounddevice); `snapshot()` for incremental streaming reads |
| `src/transcribe.py` | Local mlx-whisper transcription; `transcribe_words()` for word timestamps |
| `src/streaming.py` | Live streaming (chunk-and-finalize): bounded-window decode, volatility-margin commit |
| `src/type_text.py` | Keystroke injection at cursor (session tap, main thread, no clipboard) — streaming |
| `src/paste.py` | Clipboard paste + restore — batch mode |
| `src/bedrock_cleanup.py` | Stage 2 Bedrock cleanup (text only) + auth-error detection |
| `src/overlay.py` | Floating notch overlay pill (Listening / Transcribing / Done) |
| `src/config.py` | Config defaults + JSON persistence |
| `install.sh` | One-shot setup: python@3.12, venv, deps, model, Bedrock check, build |
| `build_app.sh` | Rebuild the `VoiceDictation.app` menu bar bundle (py2app alias mode) |
| `run.sh` | Launch in dev/script mode (perms attach to the terminal) |
| `setup.py` | py2app bundle config (menu bar agent, mic usage string) |
