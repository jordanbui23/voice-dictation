"""Isolated microphone-capture subprocess.

Runs the sounddevice InputStream in its OWN process so a wedged native
CoreAudio abort()/close() -- which blocks forever and holds the mic device --
can be recovered by the parent SIGKILLing this process. Killing the process is
the only thing that reliably frees a device stuck in a native call; an in-process
sd._terminate() would race the live native call and segfault.

Contract with the parent (all via a shared-memory ring + a control pipe):
- Frames are float32 mono, written into a preallocated shared-memory ring as a
  contiguous sample stream. The parent tracks a read cursor; this child tracks a
  write cursor and sends the new absolute write position over the pipe on every
  callback ("F", write_index, callback_count).
- The ring is oversized (parent-chosen) so a full take never wraps; if the write
  cursor ever laps the parent's last-acknowledged read cursor the child reports
  ("OVERRUN",) rather than corrupting the stream. In normal use the parent drains
  every ~0.5s so this never fires.
- On any sounddevice status flag the child forwards ("S", flag_str).
- The child imports ONLY sounddevice + numpy + stdlib. No Whisper, no rumps.
"""
import struct
import sys

import numpy as np
import sounddevice as sd


def _default_input_device():
    try:
        return sd.default.device[0]
    except (TypeError, IndexError):
        return sd.default.device


def run(shm_name, ring_samples, sample_rate, conn):
    """Child entrypoint. Opens the mic stream, pumps frames into the ring.

    Blocks until the parent sends b"stop" (or closes the pipe). All exceptions are
    reported to the parent rather than crashing silently, so the parent can decide
    to respawn.
    """
    from multiprocessing import shared_memory

    shm = shared_memory.SharedMemory(name=shm_name)
    ring = np.ndarray((ring_samples,), dtype=np.float32, buffer=shm.buf)
    write_index = 0
    callback_count = 0

    def callback(indata, frames, time_info, status):
        nonlocal write_index, callback_count
        if status:
            try:
                conn.send(("S", str(status)))
            except (BrokenPipeError, OSError):
                pass
        mono = indata.reshape(-1).astype(np.float32, copy=False)
        n = mono.size
        start = write_index % ring_samples
        end = start + n
        if end <= ring_samples:
            ring[start:end] = mono
        else:
            first = ring_samples - start
            ring[start:] = mono[:first]
            ring[: n - first] = mono[first:]
        write_index += n
        callback_count += 1
        try:
            conn.send(("F", write_index, callback_count))
        except (BrokenPipeError, OSError):
            pass

    stream = None
    try:
        stream = sd.InputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
            device=_default_input_device(),
            latency="high",
            callback=callback,
        )
        stream.start()
        conn.send(("READY",))
        while True:
            if conn.poll(0.5):
                try:
                    msg = conn.recv()
                except EOFError:
                    break
                if msg == "stop":
                    break
    except Exception as e:
        try:
            conn.send(("ERROR", repr(e)))
        except (BrokenPipeError, OSError):
            pass
    finally:
        if stream is not None:
            try:
                stream.abort()
                stream.close()
            except Exception:
                pass
        try:
            shm.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(
        "audio_capture_child is spawned by the Recorder parent, not run directly."
    )
