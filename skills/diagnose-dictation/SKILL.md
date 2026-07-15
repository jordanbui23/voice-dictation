---
name: diagnose-dictation
description: Diagnose a bad, wrong, empty, or hallucinated dictation output in the voice-dictation project by reading logs/transcripts.jsonl. Use when the user says a dictation "came out wrong", "typed garbage", "said Thanks for watching", produced a fragment when they spoke full sentences, dropped words, output nothing, or asks to "check the log" / "why did that dictation fail". Classifies the failure as mic dropout vs early release vs Whisper silence-hallucination vs Bedrock cleanup issue using the captured_s / hold_s / peak / rms / status_flags diagnostics. Do NOT use for building or relaunching the app (use build-and-relaunch) or for general feature work.
---

# Diagnose a Bad Dictation

Every dictation appends one JSON line to `logs/transcripts.jsonl`. The diagnostics fields
let you classify *why* an output was wrong instead of guessing. Read the log first — never
speculate about a dictation you haven't inspected.

## Log schema (event: "dictation")

```json
{"ts": "...", "event": "dictation", "raw": "<whisper output>", "final": "<after cleanup>",
 "cleanup": true, "cleanup_error": null,
 "captured_s": 21.67, "hold_s": 21.89, "callbacks": 23117,
 "status_flags": [], "peak": 0.1532, "rms": 0.01573}
```

- `raw` = local Whisper transcript. `final` = after Bedrock cleanup (equals `raw` if cleanup off/failed).
- `captured_s` = seconds of audio actually captured. `hold_s` = wall-clock the hotkey was held.
- `peak` / `rms` = audio level of the captured clip. `callbacks` = mic callback count. `status_flags` = sounddevice overflow/dropout flags.
- Streaming entries add `mode: "stream"`. Discarded clips log `event: "dictation_discarded"` with a `reason` (`too_short` / `empty_transcript`).

## Healthy baseline (for comparison)

Real good dictations look like: `captured_s ≈ hold_s` (within ~0.3s), `peak` ≈ 0.15–0.19,
`rms` ≈ 0.015–0.018, `status_flags: []`, `callbacks` roughly `hold_s × ~1000`.

## Step 1 — pull the offending line

```bash
cd ~/projects/voice-dictation
tail -5 logs/transcripts.jsonl | python3 -m json.tool 2>/dev/null || tail -5 logs/transcripts.jsonl
```
Match by `ts` (roughly when it happened) or by the wrong `raw`/`final` text.

## Step 2 — classify with this decision table

| Symptom in the log | Diagnosis | Fix direction |
|---|---|---|
| `hold_s` multi-second BUT `captured_s ≈ 0` and/or `peak ≈ 0`, few `callbacks` | **Mic dropout** — audio never reached Whisper (real bug) | Check mic device / permissions; investigate `Recorder` stream start; retry logic |
| `status_flags` non-empty (overflow/input flags) | **Capture instability** — dropped frames mid-record | Buffer/latency in `sd.InputStream`; system mic contention |
| `peak`/`rms` normal but `raw` is garbage like "Thanks for watching!" / "Playing the games, yes." | **Whisper silence-hallucination** — near-silent audio, model invented text | Raise no-speech threshold, use a bigger `whisper_model`, or gate on low `rms` |
| `hold_s` very short (< ~0.5s) | **Early release** — user let go before/just as they spoke | User behavior; consider a min-duration hint |
| `captured_s ≈ hold_s`, `peak` normal, `raw` correct but `final` wrong, `cleanup: true` | **Bedrock cleanup altered meaning** | Inspect/adjust the cleanup prompt in `bedrock_cleanup.py` |
| `cleanup: false` + `cleanup_error` set | **Cleanup failed → raw fallback** (working as designed) | If auth error, refresh AWS credentials; otherwise transient |
| `event: dictation_discarded`, `reason: too_short` | Clip under 0.2s, intentionally dropped | Not a bug unless it should have had audio |
| `event: dictation_discarded`, `reason: empty_transcript` | Whisper returned nothing | Usually silence; check `peak` — if normal, model/threshold issue |

## Step 3 — confirm and report

State the classification with the evidence, e.g.:
> `10:39:44` — `hold_s: 4.2` but `captured_s: 0.1`, `peak: 0.0`, `callbacks: 3` → **mic dropout**, audio never reached Whisper. Not an early release or hallucination.

## Gotchas

- **Streaming diagnostics come from `session.diagnostics`, not `recorder.snapshot()`.**
  `recorder.stop()` clears frames, so reading the recorder after stop reports a false
  `captured_s: 0`. The code already captures diagnostics at `stop()` time — trust the
  logged fields, not a fresh recorder read.
- A "fragment when I spoke full sentences" with normal `peak` is almost always
  hallucination or mid-capture dropout (check `status_flags`), NOT early release —
  `hold_s` tells you which.
- No new `dictation` line since the last build means the failure predates your current
  code; there is nothing to diagnose until it recurs. Confirm with the `ts` of the last line.

## Done when

You've named the failure class from the actual logged fields (not assumption) and pointed
to the specific fix direction — or confirmed the output was working-as-designed.
