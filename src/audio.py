"""Push-to-talk audio capture via sounddevice. 16kHz mono float32.

Supports two read modes:
- stop(): batch mode — end capture, return the whole clip.
- snapshot(): streaming mode — return the audio-so-far WITHOUT stopping, so a
  sliding-window transcriber can re-decode the growing buffer while recording.

Also records diagnostics (wall-clock hold time, captured seconds, callback count,
and any sounddevice status/overflow flags) so a short/garbled capture can be
diagnosed as early-release vs. mic dropout vs. Whisper mis-transcription.
"""
import threading
import time

import numpy as np
import sounddevice as sd


class Recorder:
    def __init__(self, sample_rate=16000):
        self.sample_rate = sample_rate
        self._frames = []
        self._lock = threading.Lock()
        self._stream = None
        self._start_time = None
        self._callback_count = 0
        self._status_flags = []

    def _callback(self, indata, frames, time_info, status):
        if status:
            self._status_flags.append(str(status))
        with self._lock:
            self._frames.append(indata.copy())
            self._callback_count += 1

    def start(self):
        with self._lock:
            self._frames = []
            self._callback_count = 0
            self._status_flags = []
        self._start_time = time.time()
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            callback=self._callback,
        )
        self._stream.start()

    def snapshot(self):
        """Return audio captured so far as 1-D float32, WITHOUT stopping capture."""
        with self._lock:
            if not self._frames:
                return np.zeros(0, dtype=np.float32)
            frames = list(self._frames)
        return np.concatenate(frames, axis=0).reshape(-1).astype(np.float32)

    def stop(self):
        """Stop capture, return 1-D float32 mono array (empty if nothing recorded)."""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        with self._lock:
            frames = list(self._frames)
            self._frames = []
        if not frames:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(frames, axis=0).reshape(-1).astype(np.float32)

    def diagnostics(self, audio):
        """Return a dict describing the just-captured audio for logging.

        audio: the array returned by stop(). Compares wall-clock hold time to
        actually-captured audio seconds; a big gap means dropped/late frames
        (mic issue), not an early release.
        """
        captured_s = round(audio.size / self.sample_rate, 2) if audio.size else 0.0
        hold_s = round(time.time() - self._start_time, 2) if self._start_time else None
        peak = float(np.abs(audio).max()) if audio.size else 0.0
        rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2))) if audio.size else 0.0
        return {
            "captured_s": captured_s,
            "hold_s": hold_s,
            "callbacks": self._callback_count,
            "status_flags": self._status_flags[:5],
            "peak": round(peak, 4),
            "rms": round(rms, 5),
        }
