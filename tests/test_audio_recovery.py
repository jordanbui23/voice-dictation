"""Recovery + transport tests for the subprocess-isolated Recorder.

Two layers:

1. Real transport (test_real_child_*): spawn the ACTUAL audio_capture_child via a
   real socketpair, but point sounddevice at a dummy so no mic is needed. Proves the
   real Popen + framing + reader-thread path delivers frames to the parent -- the
   layer three prior fixes broke without any test catching it.

2. Recovery logic (test_no_frames_*, test_*_salvage): fake at the _spawn_child level
   so we can deterministically simulate a wedged child (no frames) and a mid-record
   wedge, and assert respawn / escalation / salvage. Frames are injected the same way
   the real reader thread does: appended to rec._frames under rec._lock + generation
   guard.

Run: ./venv/bin/python tests/test_audio_recovery.py
"""
import os
import sys
import threading
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from audio import Recorder


class FakeChild:
    """Feeds frames into the parent exactly as the real reader thread does:
    append float32 to rec._frames and bump rec._callback_count, under the lock and
    generation guard. No sockets -- this fakes the post-transport delivery."""

    def __init__(self, rec, samples_per_tick=160, deliver=True):
        self.rec = rec
        self.samples_per_tick = samples_per_tick
        self.deliver = deliver
        self._stop = threading.Event()
        self.generation = None

    def start(self):
        self.generation = self.rec._generation
        threading.Thread(target=self._run, daemon=True).start()

    def stop_delivering(self):
        self.deliver = False

    def _run(self):
        while not self._stop.is_set():
            if self.deliver:
                rec = self.rec
                with rec._lock:
                    if self.generation != rec._generation:
                        return
                    rec._frames.append(
                        np.full(self.samples_per_tick, 0.05, dtype=np.float32)
                    )
                    rec._callback_count += 1
            time.sleep(0.02)

    def kill(self):
        self._stop.set()


def _install_fake(rec, deliver=True):
    def fake_spawn():
        rec._proc = object()
        rec._sock = None
        child = FakeChild(rec, deliver=deliver)
        rec._active_fake = child
        child.start()
        return True

    def fake_kill(proc=None, sock=None):
        fake = getattr(rec, "_active_fake", None)
        if fake is not None:
            fake.kill()
        rec._proc = rec._sock = None
        rec._active_fake = None

    rec._spawn_child = fake_spawn
    rec._kill_child = fake_kill


def test_real_child_delivers_frames():
    """Spawn the REAL child subprocess over a REAL socketpair; sounddevice is
    replaced with a dummy that fires the capture callback on a timer, so no mic is
    needed. Asserts frames actually cross the socket into the parent's _frames."""
    import subprocess
    import textwrap

    src = os.path.join(os.path.dirname(__file__), "..", "src")
    venv_py = os.path.join(src, "..", "venv", "bin", "python")
    # A shim that makes `import sounddevice` return a dummy InputStream driving the
    # callback with synthetic audio -- installed via a sitecustomize on PYTHONPATH.
    shim_dir = os.path.join(os.path.dirname(__file__), "_shim")
    os.makedirs(shim_dir, exist_ok=True)
    with open(os.path.join(shim_dir, "sitecustomize.py"), "w") as f:
        f.write(textwrap.dedent("""
            import sys, threading, time, types
            import numpy as np
            sd = types.ModuleType("sounddevice")
            class _Default: device = [0, 0]
            sd.default = _Default()
            class InputStream:
                def __init__(self, samplerate, channels, dtype, device, latency, callback):
                    self._cb = callback; self._stop = threading.Event()
                def start(self):
                    def run():
                        while not self._stop.is_set():
                            self._cb(np.full((160,1), 0.05, dtype=np.float32), 160, None, None)
                            time.sleep(0.01)
                    threading.Thread(target=run, daemon=True).start()
                def abort(self): self._stop.set()
                def close(self): self._stop.set()
            sd.InputStream = InputStream
            sys.modules["sounddevice"] = sd
        """))

    rec = Recorder(sample_rate=16000)
    env = dict(os.environ)
    env["PYTHONPATH"] = shim_dir + os.pathsep + env.get("PYTHONPATH", "")

    import socket
    orig_spawn = rec._spawn_child

    def spawn_with_shim():
        parent_sock, child_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        proc = subprocess.Popen(
            [os.path.abspath(venv_py), "-m", "audio_capture_child",
             str(child_sock.fileno()), "16000"],
            cwd=os.path.abspath(src), pass_fds=(child_sock.fileno(),),
            stderr=subprocess.PIPE, env=env,
        )
        child_sock.close()
        with rec._lock:
            generation = rec._generation
        reader = threading.Thread(target=rec._reader, args=(generation, parent_sock),
                                  daemon=True)
        if not rec._await_ready(parent_sock, proc):
            err = proc.stderr.read().decode() if proc.stderr else ""
            rec._kill_child(proc, parent_sock)
            raise AssertionError(f"child never READY. stderr:\n{err}")
        with rec._lock:
            rec._proc, rec._sock = proc, parent_sock
        reader.start()
        return True

    rec._spawn_child = spawn_with_shim
    assert rec.start() is True, "real child failed to start"
    time.sleep(0.4)
    audio = rec.stop()
    assert audio.size > 0, "no frames crossed the real socket from the real child"
    print(f"  real_child_delivers_frames: {audio.size} samples over real socket OK")


def test_normal_capture():
    rec = Recorder(sample_rate=16000)
    _install_fake(rec, deliver=True)
    assert rec.start() is True
    time.sleep(0.3)
    assert rec.snapshot().size > 0, "expected frames from a healthy child"
    audio = rec.stop()
    assert audio.size > 0, "stop() must return captured audio"
    print(f"  normal_capture: captured {audio.size} samples OK")


def test_no_frames_escalates():
    escalated = []
    rec = Recorder(sample_rate=16000, on_no_callbacks=lambda: escalated.append(1),
                   health_check_s=0.05)
    _install_fake(rec, deliver=False)  # child never delivers -> wedge
    assert rec.start() is True
    time.sleep(0.6)  # allow watchdog respawn attempts + escalation
    assert escalated, "watchdog must fire on_no_callbacks when no frames ever arrive"
    rec.stop()
    assert "no_callbacks_at_stop" in rec._status_flags
    print(f"  no_frames_escalates: escalated={len(escalated)} flags={rec._status_flags[:3]} OK")


def test_midrecord_salvage():
    """Frames captured before a mid-record wedge must survive stop()."""
    rec = Recorder(sample_rate=16000, health_check_s=0.05)
    _install_fake(rec, deliver=True)
    assert rec.start() is True
    time.sleep(0.3)
    with rec._lock:
        captured = sum(f.size for f in rec._frames)
    assert captured > 0
    rec._active_fake.stop_delivering()  # simulate mid-record wedge
    time.sleep(0.1)
    audio = rec.stop()
    assert audio.size >= captured, (
        f"salvage failed: had {captured} samples pre-wedge, stop() returned {audio.size}"
    )
    print(f"  midrecord_salvage: pre-wedge={captured} salvaged={audio.size} OK")


if __name__ == "__main__":
    failures = 0
    cases = [
        ("test_real_child_delivers_frames", test_real_child_delivers_frames),
        ("test_normal_capture", test_normal_capture),
        ("test_no_frames_escalates", test_no_frames_escalates),
        ("test_midrecord_salvage", test_midrecord_salvage),
    ]
    for name, fn in cases:
        try:
            fn()
        except AssertionError as e:
            failures += 1
            print(f"FAIL {name}: {e}")
        except Exception as e:
            failures += 1
            print(f"ERROR {name}: {e!r}")
    total = len(cases)
    print(f"\n{'PASS' if failures == 0 else 'FAIL'}: {total - failures}/{total} passed")
    sys.exit(1 if failures else 0)
