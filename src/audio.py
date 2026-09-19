"""Push-to-talk audio capture via an isolated subprocess. 16kHz mono float32.

The sounddevice InputStream runs in a child process (audio_capture_child.py), NOT
in this process. Frames cross into the parent through a preallocated shared-memory
ring; the child signals new write positions over a control pipe. This process only
copies float32 samples out of the ring -- it never touches PortAudio -- so a wedged
native CoreAudio call can no longer hang or poison THIS process. Recovery is a
SIGKILL + respawn of the child, which is the only thing that reliably frees a mic
device stuck in a native abort()/close(); killing a process reclaims the device via
the OS instead of racing a live native pointer (which an in-process sd._terminate()
would do -> use-after-free -> segfault).

API preserved for streaming.py / app.py:
- start()      -> bool: begin capture. False if the device could not be brought up
                  (child wedged / respawn failed) so the caller aborts the take.
- snapshot()   -> 1-D float32 of audio-so-far, WITHOUT stopping (streaming mode).
- stop()       -> 1-D float32 of the whole take (empty if nothing captured).
- diagnostics(audio) -> dict for logging (captured_s vs hold_s, peak/rms, flags).
"""
import multiprocessing as mp
import threading
import time

import numpy as np

DEFAULT_SAMPLE_RATE = 16000
RING_SECONDS = 300
HEALTH_CHECK_S = 0.3
RESPAWN_ATTEMPTS = 2


class Recorder:
    def __init__(self, sample_rate=DEFAULT_SAMPLE_RATE, on_no_callbacks=None,
                 health_check_s=HEALTH_CHECK_S):
        self.sample_rate = sample_rate
        self._ring_samples = int(RING_SECONDS * sample_rate)
        self._on_no_callbacks = on_no_callbacks
        self._health_check_s = health_check_s

        self._ctx = mp.get_context("spawn")
        self._lock = threading.Lock()
        self._frames = []
        self._read_index = 0
        self._write_index = 0
        self._callback_count = 0
        self._status_flags = []
        self._start_time = None
        self._recording = False
        self._generation = 0

        self._proc = None
        self._conn = None
        self._shm = None
        self._ring = None
        self._pump_thread = None

    def _spawn_child(self):
        """Create shared memory + child process. Returns True once child is READY."""
        from multiprocessing import shared_memory

        try:
            shm = shared_memory.SharedMemory(
                create=True, size=self._ring_samples * 4
            )
        except Exception:
            return False
        ring = np.ndarray((self._ring_samples,), dtype=np.float32, buffer=shm.buf)
        parent_conn, child_conn = self._ctx.Pipe()
        proc = self._ctx.Process(
            target=_child_entry,
            args=(shm.name, self._ring_samples, self.sample_rate, child_conn),
            daemon=True,
        )
        proc.start()
        child_conn.close()

        ready = False
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if parent_conn.poll(deadline - time.time()):
                try:
                    msg = parent_conn.recv()
                except EOFError:
                    break
                if msg and msg[0] == "READY":
                    ready = True
                    break
                if msg and msg[0] == "ERROR":
                    break
            else:
                break
        if not ready:
            self._kill_child(proc, parent_conn, shm)
            return False

        self._proc, self._conn, self._shm, self._ring = proc, parent_conn, shm, ring
        return True

    def _kill_child(self, proc=None, conn=None, shm=None):
        proc = proc if proc is not None else self._proc
        conn = conn if conn is not None else self._conn
        shm = shm if shm is not None else self._shm
        is_current = proc is self._proc
        if proc is not None and proc.is_alive():
            proc.kill()
            proc.join(1.0)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if shm is not None:
            try:
                shm.close()
                shm.unlink()
            except Exception:
                pass
        if is_current:
            self._proc = self._conn = self._shm = self._ring = None

    def _pump(self, generation, conn):
        """Drain control-pipe messages into shared state until generation changes.

        Advancing _write_index here (never in snapshot/stop) means a late message
        from a killed child's generation is ignored: the guard drops any message
        once _generation has moved on, so a respawned child cannot have its frames
        corrupted by the dead one.
        """
        while True:
            with self._lock:
                if generation != self._generation:
                    return
            try:
                if not conn.poll(0.2):
                    continue
                msg = conn.recv()
            except (EOFError, OSError):
                return
            with self._lock:
                if generation != self._generation:
                    return
                tag = msg[0]
                if tag == "F":
                    self._write_index = msg[1]
                    self._callback_count = msg[2]
                elif tag == "S":
                    self._status_flags.append(msg[1])
                elif tag == "ERROR":
                    self._status_flags.append(f"child_error:{msg[1]}")

    def _drain_ring(self):
        """Copy [read_index, write_index) out of the ring into _frames. Caller
        holds _lock. Handles wrap; flags an overrun if the unread span exceeds the
        ring (should never happen at 0.5s drain cadence with a 5-min ring)."""
        if self._ring is None:
            return
        write = self._write_index
        avail = write - self._read_index
        if avail <= 0:
            return
        if avail > self._ring_samples:
            self._status_flags.append("ring_overrun")
            self._read_index = write - self._ring_samples
            avail = self._ring_samples
        start = self._read_index % self._ring_samples
        end = start + avail
        if end <= self._ring_samples:
            self._frames.append(self._ring[start:end].copy())
        else:
            first = self._ring_samples - start
            self._frames.append(self._ring[start:].copy())
            self._frames.append(self._ring[: avail - first].copy())
        self._read_index = write

    def _watchdog(self, generation):
        """Detect a child that started but delivers no frames, and respawn it.

        A wedged mic device is exactly the case where the fresh InputStream opens
        but never fires a callback, so no ("F", ...) ever arrives and callback_count
        stays 0. After health_check_s with no frames, SIGKILL the child (freeing the
        device) and respawn, preserving the parent-held _frames buffer so audio
        captured before a mid-record wedge survives. If frames never come back,
        notify the app so the user re-holds instead of losing the take silently.
        """
        for attempt in range(RESPAWN_ATTEMPTS + 1):
            time.sleep(self._health_check_s)
            with self._lock:
                if generation != self._generation:
                    return
                if self._callback_count > 0:
                    return
                self._status_flags.append(f"no_frames_respawn_{attempt + 1}")
            if attempt == RESPAWN_ATTEMPTS:
                break
            self._kill_child()
            if not self._spawn_child():
                continue
            with self._lock:
                if generation != self._generation:
                    return
                self._resume_child()

        with self._lock:
            still_dead = self._callback_count == 0 and generation == self._generation
        if still_dead and self._on_no_callbacks is not None:
            try:
                self._on_no_callbacks()
            except Exception:
                pass

    def _resume_child(self):
        """Start the pump thread for the current child. Caller holds _lock."""
        conn, gen = self._conn, self._generation
        self._pump_thread = threading.Thread(
            target=self._pump, args=(gen, conn), daemon=True
        )
        self._pump_thread.start()

    def start(self):
        if self._proc is not None:
            self._kill_child()
        with self._lock:
            self._frames = []
            self._read_index = 0
            self._write_index = 0
            self._callback_count = 0
            self._status_flags = []
            self._generation += 1
            generation = self._generation
        self._start_time = time.time()
        if not self._spawn_child():
            with self._lock:
                self._status_flags = ["child_spawn_failed"]
            if self._on_no_callbacks is not None:
                try:
                    self._on_no_callbacks()
                except Exception:
                    pass
            return False
        with self._lock:
            self._recording = True
            self._resume_child()
        threading.Thread(
            target=self._watchdog, args=(generation,), daemon=True
        ).start()
        return True

    def snapshot(self):
        """Return audio captured so far as 1-D float32, WITHOUT stopping capture."""
        with self._lock:
            self._drain_ring()
            if not self._frames:
                return np.zeros(0, dtype=np.float32)
            return np.concatenate(self._frames, axis=0).reshape(-1).astype(np.float32)

    def stop(self):
        """Stop capture, return 1-D float32 mono array (empty if nothing recorded).

        Frames are drained from the ring BEFORE the child is killed, so the whole
        take is preserved even if the child was mid-callback.
        """
        with self._lock:
            self._drain_ring()
            frames = list(self._frames)
            self._frames = []
            if self._callback_count == 0 and self._start_time is not None:
                self._status_flags.append("no_callbacks_at_stop")
            self._recording = False
            self._generation += 1
        if self._conn is not None:
            try:
                self._conn.send("stop")
            except Exception:
                pass
        self._kill_child()
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


def _child_entry(shm_name, ring_samples, sample_rate, conn):
    from audio_capture_child import run

    run(shm_name, ring_samples, sample_rate, conn)
