"""Local Whisper transcription via mlx-whisper (Apple Silicon Metal). Audio never leaves the Mac."""
import numpy as np


class Transcriber:
    def __init__(self, model_repo):
        self.model_repo = model_repo
        self._mlx = None

    def _lazy(self):
        if self._mlx is None:
            import mlx_whisper
            self._mlx = mlx_whisper
        return self._mlx

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
        )
        return result.get("text", "").strip()

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
        )
        words = []
        for seg in result.get("segments", []):
            for w in seg.get("words", []):
                token = w.get("word", "")
                if token.strip():
                    words.append((token, float(w.get("start", 0.0)),
                                  float(w.get("end", 0.0))))
        return words
