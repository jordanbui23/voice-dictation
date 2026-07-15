---
name: build-and-relaunch
description: Rebuild and relaunch the VoiceDictation macOS menu bar app after editing its source. Use when working in the voice-dictation project and you changed anything under src/, need to apply code changes, "rebuild the app", "relaunch it", "restart voice dictation", the app "still shows old behavior" after an edit, or you need to confirm the new build is actually running. Covers the no-hot-reload trap, py2app alias-mode rebuild, ad-hoc re-signing, and verifying the live PID. Do NOT use for first-time setup (that is install.sh) or for diagnosing bad transcription output (use diagnose-dictation).
---

# Build & Relaunch VoiceDictation

The running app does **NOT hot-reload**. Any edit under `src/` requires a rebuild AND a
relaunch of the process, or you will keep testing stale code. This is the single most
common mistake when iterating on this project.

## When to use

- You edited any file under `src/` and want the change to take effect.
- The app "still does the old thing" after you changed code.
- You need to confirm which build is actually live.

## When NOT to use

- First-time environment setup → run `./install.sh` instead.
- Diagnosing wrong/hallucinated dictation output → use the `diagnose-dictation` skill.

## Procedure

Run from the project root (`~/projects/voice-dictation`).

1. **Kill the running instance** (no hot-reload, so the old process must die):
   ```bash
   pkill -f "VoiceDictation.app/Contents/MacOS" || true
   ```
2. **Rebuild the bundle** (py2app alias mode; `build_app.sh` also re-signs ad-hoc and
   re-registers with Launch Services):
   ```bash
   ./build_app.sh
   ```
3. **Relaunch**:
   ```bash
   open dist/VoiceDictation.app
   ```
4. **Verify the new build is live** — do not claim success without this:
   ```bash
   pgrep -f "VoiceDictation.app/Contents/MacOS" && echo "running" || echo "NOT running"
   ```

## Gotchas

- **Alias mode = path-locked.** The `.app` symlinks back to this project's `venv/` and
  `src/`. Never move or rename `~/projects/voice-dictation/` — it breaks both the app
  and the login item. Rebuilding in place is fine (same path).
- **Permissions live on the bundle.** Because `open dist/VoiceDictation.app` runs the
  bundle (which has its own identity), the Microphone / Accessibility / Input Monitoring
  grants persist across rebuilds. If you instead run `./run.sh` (dev mode), permissions
  attach to the launching terminal, not the app.
- **No test suite.** Verification = rebuild, relaunch, then actually dictate and inspect
  `logs/transcripts.jsonl`. "Should work" is not verification here.
- **First launch after granting a new permission** may show a macOS "Quit & Reopen"
  prompt — allow it, then relaunch.

## Done when

`pgrep` shows a live `VoiceDictation.app` PID AND a fresh dictation produces the expected
behavior (or the expected new log fields appear in `logs/transcripts.jsonl`).
