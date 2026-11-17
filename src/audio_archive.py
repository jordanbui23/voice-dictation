"""Local-only archive of dictation audio for offline model comparison.

Clips are written as 16-bit mono WAV under logs/audio/ and never leave the Mac.
Each save also deletes this archive's own clips older than keep_days, so retention
is enforced by the code that creates the files rather than by a separate sweep.
"""
import os
import time
import wave
from datetime import datetime

import numpy as np


def save_clip(audio, sample_rate, directory, keep_days):
    """Write audio to directory as <timestamp>.wav and return the file name, or None."""
    if audio is None or audio.size == 0:
        return None
    os.makedirs(directory, exist_ok=True)
    name = datetime.now().strftime("%Y%m%dT%H%M%S_%f") + ".wav"
    pcm = (np.clip(audio.astype(np.float32), -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(os.path.join(directory, name), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    _prune(directory, keep_days)
    return name


def _prune(directory, keep_days):
    cutoff = time.time() - keep_days * 86400
    for entry in os.scandir(directory):
        if entry.is_file() and entry.name.endswith(".wav") and entry.stat().st_mtime < cutoff:
            try:
                os.remove(entry.path)
            except OSError:
                pass
