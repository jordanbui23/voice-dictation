"""Inject text at the cursor via clipboard + synthetic Cmd+V, then restore clipboard.

Uses NSPasteboard for clipboard and CGEvent for the keystroke. Restoring the
previous clipboard happens after a short delay so the target app has time to read
the pasted value before we overwrite it.
"""
import threading
import time

import Quartz
from AppKit import NSPasteboard, NSStringPboardType

_V_KEYCODE = 9  # 'v'


def _read_clipboard():
    pb = NSPasteboard.generalPasteboard()
    return pb.stringForType_(NSStringPboardType)


def _write_clipboard(text):
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    if text is not None:
        pb.setString_forType_(text, NSStringPboardType)


def _send_cmd_v():
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    down = Quartz.CGEventCreateKeyboardEvent(src, _V_KEYCODE, True)
    Quartz.CGEventSetFlags(down, Quartz.kCGEventFlagMaskCommand)
    up = Quartz.CGEventCreateKeyboardEvent(src, _V_KEYCODE, False)
    Quartz.CGEventSetFlags(up, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


def paste_text(text, restore_delay=0.25):
    """Set clipboard to text, paste at cursor, restore prior clipboard afterward."""
    if not text:
        return
    previous = _read_clipboard()
    _write_clipboard(text)
    time.sleep(0.03)
    _send_cmd_v()

    def _restore():
        time.sleep(restore_delay)
        _write_clipboard(previous)

    threading.Thread(target=_restore, daemon=True).start()
