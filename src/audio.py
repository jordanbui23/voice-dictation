"""Push-to-talk audio capture via an isolated subprocess. 16kHz mono float32.

The sounddevice InputStream runs in a child process launched with subprocess.Popen
(a fresh exec of audio_capture_child.py), NOT via multiprocessing. In a pyobjc/
py2app bundle neither multiprocessing start method works: spawn re-runs the app's
main module in the child (FileNotFoundError), and fork after the Obj-C runtime is
initialized crashes the child instantly. A plain Popen of a standalone script
sidesteps both. This process only reads float32 bytes off a socket -- it never
touches PortAudio -- so a wedged native CoreAudio call can no longer hang or poison
THIS process. Recovery is SIGKILL + respawn of the child, which frees the mic device
via the OS instead of racing a live native pointer (an in-process sd._terminate()
would -> use-after-free -> segfault).

API preserved for streaming.py / app.py:
- start()      -> bool: begin capture. False if the child could not be brought up
                  (wedged / spawn failed) so the caller aborts the take.
- snapshot()   -> 1-D float32 of audio-so-far, WITHOUT stopping (streaming mode).
- stop()       -> 1-D float32 of the whole take (empty if nothing captured).
- diagnostics(audio) -> dict for logging (captured_s vs hold_s, peak/rms, flags).
"""
import os
import socket
import struct
import subprocess
import sys
import threading
import time

import numpy as np

DEFAULT_SAMPLE_RATE = 16000
HEALTH_CHECK_S = 0.3
RESPAWN_ATTEMPTS = 2
READY_TIMEOUT_S = 3.0


def _venv_python():
    """Absolute path to the venv interpreter (which has numpy + sounddevice).

    sys.executable is unreliable in the bundle (app stub / bare Homebrew python
    without the venv site-packages). The venv python sits at a fixed path relative
    to this source file: src/audio.py -> ../venv/bin/python. realpath-resolve so a
    symlinked bundle path still lands on the interpreter."""
    p = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "venv",
                     "bin", "python")
    )
    if os.path.exists(p):
        return p
    return sys.executable


class Recorder:
    def __init__(self, sample_rate=DEFAULT_SAMPLE_RATE, on_no_callbacks=None,
                 health_check_s=HEALTH_CHECK_S):
        self.sample_rate = sample_rate
        self._on_no_callbacks = on_no_callbacks
        self._health_check_s = health_check_s

        self._lock = threading.Lock()
        self._frames = []
        self._callback_count = 0
        self._status_flags = []
        self._start_time = None
        self._recording = False
        self._generation = 0

        self._proc = None
        self._sock = None
        self._reader_thread = None

    def _spawn_child(self):
        """Popen the capture child over an AF_UNIX socketpair. True once READY.

        The parent keeps one socket end; the other is passed to the child via
        pass_fds and its integer fd handed on argv. A reader thread demultiplexes
        the child's framed stream (control messages + audio) into shared state."""
        parent_sock, child_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        child_fd = child_sock.fileno()
        child_dir = os.path.dirname(os.path.abspath(__file__))
        try:
            proc = subprocess.Popen(
                [_venv_python(), "-m", "audio_capture_child",
                 str(child_fd), str(self.sample_rate)],
                cwd=child_dir,
                pass_fds=(child_fd,),
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            parent_sock.close()
            child_sock.close()
            return False
        child_sock.close()  # only the child needs its end

        with self._lock:
            generation = self._generation
        reader = threading.Thread(
            target=self._reader, args=(generation, parent_sock), daemon=True
        )

        ready = self._await_ready(parent_sock, proc)
        if not ready:
            self._kill_child(proc, parent_sock)
            return False

        with self._lock:
            self._proc, self._sock = proc, parent_sock
            self._reader_thread = reader
        reader.start()
        return True

    def _await_ready(self, sock, proc):
        """Block until the child's first framed control message is ('READY').

        Runs on the caller's thread BEFORE the reader thread starts, so it reads
        the socket directly. A control frame is b"C" + kind + payload; kind b"READY"
        means the stream is live, b"E" means the child errored during startup."""
        sock.settimeout(READY_TIMEOUT_S)
        try:
            frame = _recv_frame(sock)
        except (OSError, _FrameError):
            frame = None
        finally:
            try:
                sock.settimeout(None)
            except OSError:
                pass
        if not frame or frame[:1] != b"C":
            return False
        kind = frame[1:]
        if kind == b"READY":
            return True
        if kind[:1] == b"E":
            with self._lock:
                self._status_flags.append(
                    "child_error:" + kind[1:].decode("utf-8", "replace")
                )
        return False

    def _kill_child(self, proc=None, sock=None):
        proc = proc if proc is not None else self._proc
        sock = sock if sock is not None else self._sock
        is_current = proc is self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=1.0)
            except Exception:
                pass
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        if is_current:
            self._proc = self._sock = None

    def _reader(self, generation, sock):
        """Demux the child's framed stream into shared state until generation ends.

        Audio frames (b"F" + callback_count + float32 bytes) append to _frames and
        bump _callback_count. Control frames (b"C" + kind ...) record status flags.
        The generation guard drops everything once _generation has moved on, so a
        killed child's late frames can never corrupt a respawned child's buffer."""
        while True:
            with self._lock:
                if generation != self._generation:
                    return
            try:
                frame = _recv_frame(sock)
            except (OSError, _FrameError):
                return
            if frame is None:  # EOF: child exited
                return
            tag = frame[:1]
            with self._lock:
                if generation != self._generation:
                    return
                if tag == b"F":
                    self._callback_count = struct.unpack(">I", frame[1:5])[0]
                    samples = np.frombuffer(frame[5:], dtype=np.float32)
                    if samples.size:
                        self._frames.append(samples.copy())
                elif tag == b"C":
                    kind = frame[1:]
                    if kind[:1] == b"S":
                        self._status_flags.append(
                            kind[1:].decode("utf-8", "replace")
                        )
                    elif kind[:1] == b"E":
                        self._status_flags.append(
                            "child_error:" + kind[1:].decode("utf-8", "replace")
                        )

    def _watchdog(self, generation):
        """Detect a child that started but delivers no frames, and respawn it.

        A wedged mic device is exactly the case where the child's InputStream opens
        but never fires a callback, so no audio frame ever arrives and
        _callback_count stays 0. After health_check_s with no frames, SIGKILL the
        child (freeing the device) and respawn, preserving the parent-held _frames
        buffer so audio captured before a mid-record wedge survives. If frames never
        come back, notify the app so the user re-holds instead of losing the take.
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
            with self._lock:
                if generation != self._generation:
                    return
            if not self._spawn_child():
                continue

        with self._lock:
            still_dead = self._callback_count == 0 and generation == self._generation
        if still_dead and self._on_no_callbacks is not None:
            try:
                self._on_no_callbacks()
            except Exception:
                pass

    def start(self):
        if self._proc is not None:
            self._kill_child()
        with self._lock:
            self._frames = []
            self._callback_count = 0
            self._status_flags = []
            self._generation += 1
            generation = self._generation
        self._start_time = time.time()
        if not self._spawn_child():
            with self._lock:
                if not self._status_flags:
                    self._status_flags = ["child_spawn_failed"]
            if self._on_no_callbacks is not None:
                try:
                    self._on_no_callbacks()
                except Exception:
                    pass
            return False
        with self._lock:
            self._recording = True
        threading.Thread(
            target=self._watchdog, args=(generation,), daemon=True
        ).start()
        return True

    def snapshot(self):
        """Return audio captured so far as 1-D float32, WITHOUT stopping capture."""
        with self._lock:
            if not self._frames:
                return np.zeros(0, dtype=np.float32)
            return np.concatenate(self._frames, axis=0).reshape(-1).astype(np.float32)

    def stop(self):
        """Stop capture, return 1-D float32 mono array (empty if nothing recorded).

        The reader thread has been appending frames to _frames as they arrive, so
        the whole take is already in hand; we just snapshot it, then kill the child.
        """
        with self._lock:
            frames = list(self._frames)
            self._frames = []
            if self._callback_count == 0 and self._start_time is not None:
                self._status_flags.append("no_callbacks_at_stop")
            self._recording = False
            self._generation += 1
            sock = self._sock
        if sock is not None:
            try:
                sock.sendall(b"stop\n")
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


class _FrameError(Exception):
    pass


def _recv_exactly(sock, n):
    """Read exactly n bytes, or None on clean EOF. Loops over partial reads."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _recv_frame(sock):
    """Read one length-prefixed frame. Returns bytes, or None on EOF."""
    header = _recv_exactly(sock, 4)
    if header is None:
        return None
    (length,) = struct.unpack(">I", header)
    if length == 0:
        return b""
    payload = _recv_exactly(sock, length)
    if payload is None:
        return None
    return payload
