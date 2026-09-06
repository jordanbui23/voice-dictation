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
    def __init__(self, sample_rate=16000, on_no_callbacks=None, health_check_s=0.3):
        self.sample_rate = sample_rate
        self._frames = []
        self._lock = threading.Lock()
        self._stream = None
        self._start_time = None
        self._callback_count = 0
        self._status_flags = []
        self._on_no_callbacks = on_no_callbacks
        self._health_check_s = health_check_s
        self._generation = 0
        self._needs_reset = False
        self._reset_lock = threading.Lock()
        self._teardown_threads = []
        self._start_retries = 3

    def _callback(self, indata, frames, time_info, status):
        if status:
            self._status_flags.append(str(status))
        with self._lock:
            self._frames.append(indata.copy())
            self._callback_count += 1

    def _default_input_device(self):
        try:
            return sd.default.device[0]
        except (TypeError, IndexError):
            return sd.default.device

    def _watchdog(self, generation, stream):
        """Detect a poisoned device: a stream that starts but delivers no audio.

        After a wedged teardown the mic device can stay held by an abandoned
        stream, so a fresh InputStream starts cleanly yet never fires _callback.
        The clip is then silently discarded as too-short. Catch it early: if no
        callbacks arrived within health_check_s, try re-opening the stream a few
        times (the device sometimes frees once the abandoned abort() lands),
        flagging the device for a full PortAudio reset on the next start(). Runs
        on its own daemon thread, so the re-open attempts never block the hotkey
        thread. If audio never returns, tear the dead stream down and notify the
        app so the user retries instead of losing the whole take.
        """
        time.sleep(self._health_check_s)
        with self._lock:
            if generation != self._generation:
                return
            if self._callback_count > 0:
                return
            self._status_flags.append("no_callbacks")
            self._needs_reset = True

        for attempt in range(self._start_retries):
            with self._lock:
                if generation != self._generation:
                    return
                dead, self._stream = self._stream, None
            self._teardown_stream(dead, timeout_s=1.0)
            with self._lock:
                if generation != self._generation:
                    return
                self._status_flags.append(f"watchdog_retry_{attempt + 1}")
            try:
                new_stream = self._open_stream(generation)
            except Exception:
                break
            with self._lock:
                if generation != self._generation:
                    stream = new_stream
                    break
                self._stream = stream = new_stream
                self._start_time = time.time()
            time.sleep(self._health_check_s)
            with self._lock:
                if generation != self._generation:
                    return
                if self._callback_count > 0:
                    self._needs_reset = False
                    return

        with self._lock:
            if self._stream is stream:
                self._stream = None
        self._teardown_stream(stream, timeout_s=1.0)
        if self._on_no_callbacks is not None:
            try:
                self._on_no_callbacks()
            except Exception:
                pass

    def _recover_device(self):
        """Release a mic device poisoned by an abandoned/wedged stream.

        Tiered so the heavy global PortAudio re-init only runs when it is safe.
        A prior teardown that wedged in CoreAudio leaves the device held; the
        next InputStream then opens "successfully" but never fires _callback.
        We must not run sd._terminate() while an orphaned abort()/close()
        thread is still alive on that device -- terminate frees the stream out
        from under it, and the orphan's pending native close() then runs on
        freed state (segfault). So: only re-init when no teardown orphan is
        alive; otherwise defer and let a bounded start() retry try to recover.
        """
        with self._lock:
            if not self._needs_reset:
                return
            orphan_alive = any(t.is_alive() for t in self._teardown_threads)
        if orphan_alive:
            return
        if not self._reset_lock.acquire(blocking=False):
            return
        try:
            def run():
                try:
                    sd._terminate()
                    sd._initialize()
                except Exception:
                    pass

            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            worker.join(2.0)
            if worker.is_alive():
                with self._lock:
                    self._status_flags.append("recover_terminate_timeout")
                    self._needs_reset = True
        finally:
            self._reset_lock.release()

    def _open_stream(self, generation):
        with self._lock:
            self._frames = []
            self._callback_count = 0
        stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            device=self._default_input_device(),
            latency="high",
            callback=self._callback,
        )
        stream.start()
        return stream

    def start(self):
        self._recover_device()
        with self._lock:
            self._frames = []
            self._callback_count = 0
            self._status_flags = []
            self._generation += 1
            generation = self._generation
        self._start_time = time.time()
        self._stream = self._open_stream(generation)
        threading.Thread(
            target=self._watchdog,
            args=(generation, self._stream),
            daemon=True,
        ).start()

    def snapshot(self):
        """Return audio captured so far as 1-D float32, WITHOUT stopping capture."""
        with self._lock:
            if not self._frames:
                return np.zeros(0, dtype=np.float32)
            frames = list(self._frames)
        return np.concatenate(frames, axis=0).reshape(-1).astype(np.float32)

    def _teardown_stream(self, stream, timeout_s=2.0):
        """Abort and close a PortAudio stream, abandoning it if it wedges.

        _stream.abort()/close() are native CoreAudio calls that can block
        indefinitely when the mic device is in a bad state. A wedged call here
        used to latch the app's processing flag forever. abort() discards
        buffered audio and returns faster than stop() (which drains) -- frames
        are already salvaged before teardown, so draining buys nothing. Run it
        on a daemon thread and join with a timeout; if it doesn't return, drop
        the reference (the thread dies with the app) so the next start()
        creates a fresh stream.
        """
        if stream is None:
            return

        def run():
            try:
                stream.abort()
                stream.close()
            except Exception:
                pass

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        with self._lock:
            self._teardown_threads = [t for t in self._teardown_threads if t.is_alive()]
            self._teardown_threads.append(worker)
        worker.join(timeout_s)
        if worker.is_alive():
            self._status_flags.append(f"stream_teardown_timeout_{timeout_s}s")
            with self._lock:
                self._needs_reset = True

    def stop(self):
        """Stop capture, return 1-D float32 mono array (empty if nothing recorded).

        Frames are salvaged BEFORE stream teardown so a wedged stop/close still
        yields whatever audio was captured instead of dropping the clip.
        """
        with self._lock:
            frames = list(self._frames)
            self._frames = []
            if self._callback_count == 0 and self._start_time is not None:
                self._status_flags.append("no_callbacks_at_stop")
                self._needs_reset = True
            self._generation += 1
        stream, self._stream = self._stream, None
        self._teardown_stream(stream)
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
