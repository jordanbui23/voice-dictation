"""Local Whisper transcription via mlx-whisper (Apple Silicon Metal). Audio never leaves the Mac."""
import re
import threading

import numpy as np

_TOKEN_RE = re.compile(r"\s+")


def _collapse_exact_ngrams(tokens, max_repeats, max_ngram):
    """Collapse an exact token n-gram repeated more than max_repeats times in a row."""
    for n in range(1, max_ngram + 1):
        if len(tokens) < n * (max_repeats + 1):
            continue
        out = []
        i = 0
        while i < len(tokens):
            gram = tokens[i:i + n]
            if len(gram) < n:
                out.extend(gram)
                break
            reps = 1
            j = i + n
            while j + n <= len(tokens) and tokens[j:j + n] == gram:
                reps += 1
                j += n
            if reps > max_repeats:
                out.extend(gram)
                i = j
            else:
                out.append(tokens[i])
                i += 1
        tokens = out
    return tokens


def collapse_repetitions(text, max_repeats=3, max_ngram=5):
    """Collapse a token n-gram repeated more than max_repeats times in a row.

    Whisper degenerates into repetition loops (e.g. 'Cat, Hanhee, Cat, Hanhee,
    ...' x40) on ambiguous or quiet audio. This local guard runs before paste in
    both batch and streaming modes, independent of the Bedrock cleanup stage, so
    the loop never reaches the cursor even when cleanup is off or falls back to raw.

    Deliberately conservative: only an EXACT n-gram repeated beyond max_repeats is
    collapsed, so a genuine emphatic repetition in real speech ('no no no you should
    get the perm') is preserved. A substitution-tolerant pass was tried and rejected
    for corrupting real speech while still missing exotic interleaved loops; the
    Bedrock cleanup stage handles the residual when enabled.

    Order-preserving; a phrase repeated up to max_repeats times is left untouched.
    """
    if not text:
        return text
    tokens = _TOKEN_RE.split(text.strip())
    if len(tokens) <= max_repeats:
        return text
    tokens = _collapse_exact_ngrams(tokens, max_repeats, max_ngram)
    return " ".join(tokens)


class Transcriber:
    def __init__(self, model_repo, initial_prompt="", metal_cache_limit_gb=1.0):
        self.model_repo = model_repo
        self.initial_prompt = initial_prompt or ""
        self.metal_cache_limit_gb = metal_cache_limit_gb
        self._mlx = None

    def _lazy(self):
        if self._mlx is None:
            import mlx.core as mx
            import mlx_whisper
            if self.metal_cache_limit_gb is not None:
                mx.set_cache_limit(int(self.metal_cache_limit_gb * 1024 ** 3))
            self._mlx = mlx_whisper
        return self._mlx

    def _decode_kwargs(self):
        kwargs = {"temperature": 0.0, "condition_on_previous_text": False}
        if self.initial_prompt:
            kwargs["initial_prompt"] = self.initial_prompt
        return kwargs

    def warm_up(self):
        """Force model download/load so first real dictation isn't slow."""
        self._lazy().transcribe(
            np.zeros(16000, dtype=np.float32),
            path_or_hf_repo=self.model_repo,
        )

    def transcribe(self, audio_f32_mono_16k):
        """audio: 1-D float32 numpy array, 16kHz mono. Returns stripped text."""
        if audio_f32_mono_16k.dtype != np.float32:
            audio_f32_mono_16k = audio_f32_mono_16k.astype(np.float32)
        result = self._lazy().transcribe(
            audio_f32_mono_16k,
            path_or_hf_repo=self.model_repo,
            fp16=True,
            **self._decode_kwargs(),
        )
        return result.get("text", "").strip()

    def transcribe_with_timeout(self, audio_f32_mono_16k, timeout_s):
        """Run transcribe on a daemon thread; raise TimeoutError if it exceeds timeout_s.

        mlx_whisper is a native Metal call that a Python thread cannot interrupt.
        On timeout the underlying thread is abandoned (daemon, dies with the app);
        this only stops the caller from waiting so the app can recover.
        """
        result = {}

        def run():
            try:
                result["text"] = self.transcribe(audio_f32_mono_16k)
            except BaseException as e:  # noqa: BLE001 - propagate to caller
                result["error"] = e

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(timeout_s)
        if worker.is_alive():
            raise TimeoutError(
                f"transcribe exceeded {timeout_s}s; abandoning wedged native call"
            )
        if "error" in result:
            raise result["error"]
        return result.get("text", "")

    def transcribe_words(self, audio_f32_mono_16k):
        """Transcribe with word-level timestamps.

        Returns a list of (word, start_sec, end_sec) tuples. Times are relative to
        the START of the passed audio buffer. Used by streaming chunk-and-finalize
        to decide which words have aged past the volatile margin and are safe to
        commit permanently.
        """
        if audio_f32_mono_16k.dtype != np.float32:
            audio_f32_mono_16k = audio_f32_mono_16k.astype(np.float32)
        result = self._lazy().transcribe(
            audio_f32_mono_16k,
            path_or_hf_repo=self.model_repo,
            fp16=True,
            word_timestamps=True,
            **self._decode_kwargs(),
        )
        words = []
        for seg in result.get("segments", []):
            for w in seg.get("words", []):
                token = w.get("word", "")
                if token.strip():
                    words.append((token, float(w.get("start", 0.0)),
                                  float(w.get("end", 0.0))))
        return words
