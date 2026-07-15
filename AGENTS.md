# AGENTS.md

Operational guide for AI agents working in this repo. For the human-facing product
docs, see [README.md](README.md) — this file is the short, rule-heavy version.

## What this is

A macOS menu bar dictation app for Apple Silicon. Hold a hotkey, speak, release,
text lands at the cursor. Two modes, auto-selected by the **Cleanup** toggle:

- **Cleanup ON → batch:** local Whisper transcribes the whole clip on release,
  Bedrock (Claude Haiku) polishes it, result is pasted at the cursor (clipboard
  saved & restored). Default.
- **Cleanup OFF → streaming:** local Whisper chunk-and-finalize types stable words
  at the cursor as you speak, via synthesized keystrokes. Fully offline, no network,
  no clipboard.

**Privacy invariant (do not break):** Stage 1 transcription is 100% local (`mlx-whisper`,
Metal). Audio NEVER leaves the Mac in either mode. Stage 2 (Bedrock) sends **raw
transcript text only** — never audio — and is toggleable.

## Build / run (there is NO test suite — validate by running the app)

```bash
./install.sh                    # one-shot: python@3.12, venv, deps, model, Bedrock check, build
./build_app.sh                  # rebuild the .app bundle after editing src/ (py2app alias mode)
open dist/VoiceDictation.app    # run as menu bar app (permissions persist to the bundle)
./run.sh                        # dev/script mode (permissions attach to the terminal instead)
```

After editing anything under `src/`, rebuild with `./build_app.sh` and relaunch —
the running process does NOT hot-reload. Confirm the new build is live with
`pgrep -f VoiceDictation.app`.

There are no unit tests. Verification = actually dictate and inspect
`logs/transcripts.jsonl`. "Should work" is not verification here.

## Hard rules

- **Never move or delete `~/projects/voice-dictation/`.** The `.app` is a py2app
  **alias-mode** bundle that symlinks back to this project's `venv/` and `src/`, and
  is registered as a macOS Login Item. Moving the dir breaks the app and auto-start.
  Rebuilding in place (`./build_app.sh`) is fine — same path.
- **Never commit `config.json`, `venv/`, `build/`, `dist/`, or `logs/`.** All are
  `.gitignore`d. `config.json` holds machine-local settings; the committed defaults
  live in `src/config.py` (`DEFAULTS`).
- **Git identity is personal** (`stajordansta@gmail.com`, auto-applied under
  `~/projects/`). This is a **personal repo**, separate from `~/workplace/` work rules.
- **This repo is PUBLIC** (`github.com/jordanbui23/voice-dictation`). Keep it
  sanitized: NO Amazon-internal identifiers in tracked files OR git history — no AWS
  account IDs/names (e.g. Cecelia), profile names, Midway/`mwinit`, or internal project
  jargon (Maestro, Isengard, Taskei, StackSets, Orchestrator, Executor, Brazil). Real
  operational values live ONLY in the gitignored `config.json`; committed docs/defaults
  stay generic ("configure your own AWS profile with Bedrock access"). Before adding any
  AWS/Bedrock/auth text, write it generically from the start. If internal strings ever
  land in a commit, they must be scrubbed from history (squash/rewrite) before pushing,
  not just from the working tree.
- **Never `git push` from the agent** — the environment blocks it. Commit locally when
  asked, then hand the user the push command: `cd ~/projects/voice-dictation && git push origin main`.
- **Don't add code comments** unless genuinely non-obvious (matches the user's global
  style rule). Prefer clear names over narration.

## AWS / Bedrock (Stage 2 cleanup)

- Configure your own AWS profile and region in `config.json` (defaults in
  `src/config.py`): any local profile with Bedrock `InvokeModel` access, region
  `us-west-2`.
- Model: `us.anthropic.claude-haiku-4-5-20251001-v1:0` — a **cross-region inference
  profile**. If your account's role only allows `InvokeModel` via inference profiles,
  NOT bare foundation-model IDs, keep the `us.` prefix. Do not "simplify" it to
  `anthropic.claude-...`; it will 403.
- Auth is standard AWS credentials. Expired creds → `bedrock_cleanup.is_auth_error`
  catches it, the app shows a "refresh your AWS credentials" modal, and falls back to
  the raw transcript. Words are never lost. Cleanup also times out at 3s → raw fallback.

## Layout

| File | Role |
|------|------|
| `src/app.py` | Menu bar app; wires pipeline, mode selection, overlay, auth modal, logging |
| `src/hotkey.py` | Global hold-to-talk via CGEventTap (pyobjc); supports modifier combos |
| `src/audio.py` | 16 kHz mono capture (sounddevice); `snapshot()` for streaming; `diagnostics()` |
| `src/transcribe.py` | Local mlx-whisper; `transcribe()` and `transcribe_words()` |
| `src/streaming.py` | Streaming (chunk-and-finalize): bounded-window decode, volatility-margin commit |
| `src/type_text.py` | Keystroke injection at cursor (session tap, main thread, modifiers cleared) |
| `src/paste.py` | Clipboard paste + restore (batch mode) |
| `src/bedrock_cleanup.py` | Stage 2 Bedrock cleanup (text only) + auth-error classifier |
| `src/overlay.py` | Floating notch overlay pill (Listening / Transcribing / Done) |
| `src/config.py` | `DEFAULTS`, `WHISPER_MODELS`, `HOTKEYS`; JSON load/save |
| `install.sh` / `build_app.sh` / `run.sh` / `setup.py` | setup, bundle build, dev launch, py2app config |

## Gotchas learned the hard way

- **Read `session.diagnostics`, NOT `recorder.snapshot()` in `_finish_stream`.**
  `recorder.stop()` clears frames, so reading the recorder after stop reports a false
  `captured_s: 0`. Streaming diagnostics are captured into `session.diagnostics` at
  `stop()` time; the batch path reads `recorder.diagnostics(audio)` before discard.
- **Whisper "Thanks for watching!" / "Playing the games, yes." on silence = the
  silence-hallucination signature**, not a bug in this code. It means little/no audio
  reached Whisper. The diagnostics fields exist to tell these apart:
  - `captured_s ≈ 0` / `peak ≈ 0` with a multi-second `hold_s` → **mic dropout** (real bug)
  - normal `peak`/`rms` but garbage text → **Whisper hallucination** (raise no-speech
    threshold or use a bigger model)
  - short `hold_s` → **early release** (user let go too soon)
- Every dictation logs one JSON line to `logs/transcripts.jsonl` (`event: "dictation"`)
  with `raw`, `final`, `cleanup`, `mode`, and the diagnostics fields. Discarded clips
  log `event: "dictation_discarded"` with a `reason`. Inspect this file to debug output.
- Keystroke injection clears modifier flags on the main thread, so a held modifier
  hotkey (e.g. Right Cmd + Right Option) doesn't turn typed characters into shortcuts.
- First-run needs three macOS grants: **Input Monitoring** (hotkey), **Microphone**
  (only prompts on first record), **Accessibility** (type/paste). Granted to the
  `.app` bundle, or to the terminal if launched via `./run.sh`.

## When adding a feature

Match the existing structure: hotkey callbacks run on the CGEventTap thread and must
stay trivial (flip state, hand off to a worker thread) — never block them with
transcribe/cleanup/paste work. Persist any new setting via `Config` + a `DEFAULTS`
entry, and surface it in the menu bar dropdown in `app._build_menu` if user-facing.
