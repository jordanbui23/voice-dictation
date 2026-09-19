"""Recovery tests for the subprocess-isolated Recorder.

We cannot drive a real CoreAudio wedge deterministically, so these tests replace
the child-spawn + control-pipe with an in-process fake whose frame delivery we
control. That lets us assert the PARENT's contract:
  - normal frames -> snapshot()/stop() return the captured audio
  - no frames at start -> watchdog respawns, then escalates via on_no_callbacks
  - frames stop mid-record -> parent keeps already-captured audio (salvage)

Run: ./venv/bin/python tests/test_audio_recovery.py
"""
import os
import sys
import threading
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import audio as audio_mod
from audio import Recorder


class FakeChild:
    """Stands in for the capture subprocess. Writes frames straight into the
    parent's ring and advances the parent's _write_index/_callback_count the same
    way the real _pump would, under the parent's lock and generation guard."""

    def __init__(self, rec, frames_per_tick=160, deliver=True):
        self.rec = rec
        self.frames_per_tick = frames_per_tick
        self.deliver = deliver
        self._stop = threading.Event()
        self._thread = None
        self.generation = None

    def start(self):
        self.generation = self.rec._generation
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop_delivering(self):
        self.deliver = False

    def _run(self):
        while not self._stop.is_set():
            if self.deliver:
                rec = self.rec
                with rec._lock:
                    if self.generation != rec._generation:
                        return
                    w = rec._write_index % rec._ring_samples
                    n = self.frames_per_tick
                    chunk = np.full(n, 0.05, dtype=np.float32)
                    end = w + n
                    if end <= rec._ring_samples:
                        rec._ring[w:end] = chunk
                    else:
                        first = rec._ring_samples - w
                        rec._ring[w:] = chunk[:first]
                        rec._ring[: n - first] = chunk[first:]
                    rec._write_index += n
                    rec._callback_count += 1
            time.sleep(0.02)

    def kill(self):
        self._stop.set()


def _install_fake(rec, deliver=True, container=None):
    """Patch rec._spawn_child to attach a FakeChild instead of a real process.
    Allocates the ring in-process (no shared_memory needed for the fake)."""
    def fake_spawn():
        rec._ring = np.zeros(rec._ring_samples, dtype=np.float32)
        rec._proc = object()
        rec._conn = None
        rec._shm = None
        child = FakeChild(rec, deliver=deliver)
        if container is not None:
            container.append(child)
        rec._active_fake = child
        child.start()
        return True

    def fake_kill(proc=None, conn=None, shm=None):
        fake = getattr(rec, "_active_fake", None)
        if fake is not None:
            fake.kill()
        rec._proc = rec._conn = rec._shm = rec._ring = None
        rec._active_fake = None

    def fake_resume():
        pass  # the FakeChild writes shared state directly; no pump needed

    rec._spawn_child = fake_spawn
    rec._kill_child = fake_kill
    rec._resume_child = fake_resume


def test_normal_capture():
    rec = Recorder(sample_rate=16000)
    _install_fake(rec, deliver=True)
    assert rec.start() is True
    time.sleep(0.3)
    snap = rec.snapshot()
    assert snap.size > 0, "expected frames from a healthy child"
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
    audio = rec.stop()
    assert "no_callbacks_at_stop" in rec._status_flags
    print(f"  no_frames_escalates: escalated={len(escalated)} flags={rec._status_flags[:3]} OK")


def test_midrecord_salvage():
    rec = Recorder(sample_rate=16000, health_check_s=0.05)
    _install_fake(rec, deliver=True)
    assert rec.start() is True
    time.sleep(0.3)
    before = rec.snapshot().size
    assert before > 0
    rec._active_fake.stop_delivering()  # simulate mid-record wedge
    time.sleep(0.2)
    audio = rec.stop()
    assert audio.size >= before, (
        f"salvage failed: had {before} samples pre-wedge, stop() returned {audio.size}"
    )
    print(f"  midrecord_salvage: pre-wedge={before} salvaged={audio.size} OK")


def test_stop_drains_undrained_ring():
    """The salvage guarantee that matters: frames written to the ring but NEVER
    pulled by a snapshot() must still be returned by stop(). Streaming calls
    snapshot() so the ring stays near-empty, but batch mode calls ONLY stop() --
    every captured sample is undrained until then. This is the path that goes red
    if stop() stops draining before killing the child."""
    rec = Recorder(sample_rate=16000, health_check_s=0.05)
    _install_fake(rec, deliver=True)
    assert rec.start() is True
    time.sleep(0.3)  # frames accumulate in the ring; NO snapshot() call
    with rec._lock:
        undrained = rec._write_index - rec._read_index
    assert undrained > 0, "expected undrained frames in the ring before stop()"
    audio = rec.stop()
    assert audio.size >= undrained, (
        f"stop() must salvage the undrained ring: {undrained} pending, "
        f"got {audio.size}"
    )
    print(f"  stop_drains_undrained_ring: pending={undrained} returned={audio.size} OK")


if __name__ == "__main__":
    failures = 0
    cases = [
        ("test_normal_capture", test_normal_capture),
        ("test_no_frames_escalates", test_no_frames_escalates),
        ("test_midrecord_salvage", test_midrecord_salvage),
        ("test_stop_drains_undrained_ring", test_stop_drains_undrained_ring),
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
