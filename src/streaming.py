"""Streaming (live) dictation via chunk-and-finalize Whisper re-transcription.

Whisper is a batch model, so live streaming is faked by re-transcribing recent
audio on an interval and TYPING text at the cursor (keystrokes, no clipboard).
Text is append-only: once typed it is never rewritten or backspaced.

Chunk-and-finalize (chosen over sliding-window text-merge because merge has an
irreducible ambiguous-realignment failure on repeated words that would produce
visible duplicates/drops in the user's documents):

- Track a `finalized` sample offset. Audio before it is committed forever and is
  NEVER re-decoded, so there is no alignment/merge step and no way to disagree
  with already-typed text.
- Each tick, decode only [finalized - CONTEXT_PAD : now] via word timestamps -
  a bounded window, so decode time stays roughly constant regardless of total
  recording length (fixes the "long ramble gets laggy" problem).
- Convert word timestamps to absolute sample offsets; discard words that end at or
  before `finalized` (those are re-decoded context, already typed).
- Commit only words that have aged past VOLATILE_MARGIN seconds of trailing audio
  (Whisper revises the most recent ~1-2s), preferring to cut at a silence gap.
  Force a cut at MAX_CHUNK seconds so a non-stop talker still makes progress and
  decode stays bounded.
- On stop(), decode the unfinalized tail once and commit everything (no hold-back).
"""
import threading
import time

DEFAULT_INTERVAL = 0.5
CONTEXT_PAD_SECONDS = 4.0
VOLATILE_MARGIN_SECONDS = 1.5
MAX_CHUNK_SECONDS = 12.0
MIN_SILENCE_SECONDS = 0.4


class StreamingSession:
    def __init__(self, recorder, transcriber, emit, sample_rate=16000,
                 interval=DEFAULT_INTERVAL, debug=None):
        self.recorder = recorder
        self.transcriber = transcriber
        self.emit = emit
        self.sample_rate = sample_rate
        self.interval = interval
        self.debug = debug or (lambda *a, **k: None)
        self._stop = threading.Event()
        self._thread = None
        self._finalized = 0
        self._committed_any = False
        self._full_text = ""
        self.diagnostics = {}

    def start(self):
        self._stop.clear()
        self._finalized = 0
        self._committed_any = False
        self._full_text = ""
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def request_stop(self):
        """Signal the loop to stop immediately. Safe to call anytime, never blocks."""
        self._stop.set()

    def _decode_window(self, audio):
        """Decode [finalized - pad : end] of audio; return words with ABSOLUTE
        sample offsets, filtered to those ending after the finalized point."""
        pad = int(CONTEXT_PAD_SECONDS * self.sample_rate)
        win_start = max(0, self._finalized - pad)
        chunk = audio[win_start:]
        if chunk.size < self.sample_rate * 0.3:
            return []
        words = self.transcriber.transcribe_words(chunk)
        out = []
        for token, start_s, end_s in words:
            start_smp = win_start + int(start_s * self.sample_rate)
            end_smp = win_start + int(end_s * self.sample_rate)
            if end_smp > self._finalized:
                out.append((token, start_smp, end_smp))
        return out

    def _choose_cut(self, words, now_sample, force):
        """Return count of leading words safe to commit this tick, or 0.

        A word is committable only if >= VOLATILE_MARGIN of audio follows its end.
        Prefer to cut at the latest silence gap among committable words; if forced
        (window too long), commit all committable words.
        """
        margin = int(VOLATILE_MARGIN_SECONDS * self.sample_rate)
        committable = [i for i, w in enumerate(words)
                       if now_sample - w[2] >= margin]
        if not committable:
            return 0
        last_committable = committable[-1]
        min_gap = int(MIN_SILENCE_SECONDS * self.sample_rate)
        for i in range(last_committable, 0, -1):
            gap = words[i][1] - words[i - 1][2]
            if gap >= min_gap:
                return i
        if force:
            return last_committable + 1
        return 0

    def _commit_words(self, words):
        text = "".join(w[0] for w in words).strip()
        if not text:
            return
        prefix = " " if self._committed_any else ""
        self.emit(prefix + text)
        self._committed_any = True
        self._finalized = words[-1][2]
        self._full_text = (self._full_text + " " + text).strip() if self._full_text else text

    def _loop(self):
        ticks = 0
        try:
            while not self._stop.is_set():
                cycle_start = time.time()
                ticks += 1
                audio = self.recorder.snapshot()
                now_sample = audio.size
                words = self._decode_window(audio)
                self.debug("tick", tick=ticks, words=len(words),
                           finalized_s=round(self._finalized / self.sample_rate, 1),
                           decode_ms=int((time.time() - cycle_start) * 1000))
                if words:
                    unfinalized = now_sample - self._finalized
                    force = unfinalized >= int(MAX_CHUNK_SECONDS * self.sample_rate)
                    cut = self._choose_cut(words, now_sample, force)
                    if cut > 0:
                        self._commit_words(words[:cut])
                        self.debug("commit", cut=cut,
                                   text=self._full_text[-60:])
                remaining = self.interval - (time.time() - cycle_start)
                self._stop.wait(max(0.05, remaining))
        except Exception as e:
            self.debug("loop_crashed", error=repr(e))

    def stop(self):
        """Stop the loop, decode the unfinalized tail once, commit all of it.

        Captures recorder diagnostics on the final audio before frames are cleared,
        exposed via self.diagnostics for the app to log.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        audio = self.recorder.stop()
        self.diagnostics = self.recorder.diagnostics(audio)
        if audio.size < self.sample_rate * 0.2:
            return self._full_text
        words = self._decode_window(audio)
        if words:
            self._commit_words(words)
        return self._full_text
