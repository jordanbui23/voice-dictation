"""Isolated microphone-capture subprocess (standalone `python -m` entry).

Runs the sounddevice InputStream in its OWN process, launched by the parent via
subprocess.Popen (a fresh exec) -- NOT multiprocessing. In a pyobjc/py2app bundle
neither multiprocessing start method works: spawn re-runs the app's main module in
the child (FileNotFoundError), and fork after the Obj-C runtime is initialized
crashes instantly. A plain Popen of this standalone script sidesteps both: it has
its own trivial __main__ and is a clean exec with no inherited Obj-C state.

Why isolate at all: a wedged native CoreAudio abort()/close() blocks forever and
holds the mic device; the only reliable recovery is for the parent to SIGKILL this
process (an in-process sd._terminate() races the live native call -> segfault).

Wire protocol (over an inherited AF_UNIX socket whose fd is passed in argv):
- Control messages parent<-child are length-prefixed pickles: (">I" length)+payload.
  ("READY",)                     once the stream is live
  ("S", flag_str)                a sounddevice status/overflow flag
  ("ERROR", repr)                fatal error before/while streaming
- Audio frames parent<-child are length-prefixed raw float32 bytes, tag "F":
  (">I" total_len) + b"F" + (">I" callback_count) + float32_bytes
The single framed stream carries both; the parent demultiplexes on the leading tag.
- Parent->child: a single b"stop" line (newline-terminated) requests shutdown.
- If the socket read returns EOF (parent gone), the child self-exits so it can never
  orphan-hold the mic.

Imports ONLY sounddevice + numpy + stdlib. No Whisper, no rumps, no multiprocessing.
"""
import os
import queue
import socket
import struct
import sys
import threading

import numpy as np
import sounddevice as sd


def _default_input_device():
    try:
        return sd.default.device[0]
    except (TypeError, IndexError):
        return sd.default.device


def _send_frame(sock, payload):
    """Send one length-prefixed frame. Returns False if the peer is gone."""
    try:
        sock.sendall(struct.pack(">I", len(payload)) + payload)
        return True
    except (BrokenPipeError, OSError):
        return False


def run(sock, sample_rate):
    """Open the mic stream; stream frames over sock until stop/EOF/error."""
    outbox = queue.Queue(maxsize=256)
    callback_count = 0

    def callback(indata, frames, time_info, status):
        nonlocal callback_count
        if status:
            try:
                outbox.put_nowait(("S", str(status)))
            except queue.Full:
                pass
        mono = indata.reshape(-1).astype(np.float32, copy=False)
        callback_count += 1
        payload = b"F" + struct.pack(">I", callback_count) + mono.tobytes()
        try:
            outbox.put_nowait(("D", payload))
        except queue.Full:
            pass  # parent stalled/gone; never block the CoreAudio callback thread

    def reader():
        """Exit the process if the parent closes the socket (EOF) or says stop."""
        try:
            while True:
                data = sock.recv(64)
                if not data or b"stop" in data:
                    break
        except OSError:
            pass
        os._exit(0)

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
        threading.Thread(target=reader, daemon=True).start()
        _send_frame(sock, b"C" + b"READY")
        while True:
            tag, body = outbox.get()
            if tag == "D":
                if not _send_frame(sock, body):
                    break
            elif tag == "S":
                if not _send_frame(sock, b"C" + b"S" + body.encode("utf-8", "replace")):
                    break
    except Exception as e:
        try:
            _send_frame(sock, b"C" + b"E" + repr(e).encode("utf-8", "replace"))
        except Exception:
            pass
    finally:
        if stream is not None:
            try:
                stream.abort()
                stream.close()
            except Exception:
                pass
        try:
            sock.close()
        except Exception:
            pass


def _main(argv):
    fd = int(argv[1])
    sample_rate = int(argv[2])
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, fileno=fd)
    run(sock, sample_rate)


if __name__ == "__main__":
    _main(sys.argv)
