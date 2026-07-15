"""Inject text at the cursor by synthesizing keystrokes (CGEvent), no clipboard.

Used by streaming mode: as words stabilize they are typed directly. Uses
CGEventKeyboardSetUnicodeString so arbitrary characters (punctuation, unicode)
are emitted without per-character keycode mapping. The clipboard is never touched,
so streaming mode leaves the user's clipboard fully intact.

Injection is marshalled onto the main thread. CGEvent posting from a raw worker
thread (no run loop) while a hotkey CGEventTap is live proved unreliable; the main
thread has rumps' run loop, which makes posting reliable. Events post to the
session tap (above our own HID-level hotkey tap) with modifier flags cleared so a
physically-held hotkey never turns typed letters into shortcuts.
"""
import time

import Quartz
from Foundation import NSObject


_PER_CHAR_DELAY = 0.005


def _post(text):
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    for ch in text:
        down = Quartz.CGEventCreateKeyboardEvent(src, 0, True)
        Quartz.CGEventKeyboardSetUnicodeString(down, 1, ch)
        Quartz.CGEventSetFlags(down, 0)
        Quartz.CGEventPost(Quartz.kCGSessionEventTap, down)
        up = Quartz.CGEventCreateKeyboardEvent(src, 0, False)
        Quartz.CGEventKeyboardSetUnicodeString(up, 1, ch)
        Quartz.CGEventSetFlags(up, 0)
        Quartz.CGEventPost(Quartz.kCGSessionEventTap, up)
        time.sleep(_PER_CHAR_DELAY)


class _MainThreadTyper(NSObject):
    def typeText_(self, text):
        _post(text)


_typer = _MainThreadTyper.alloc().init()


def type_text(text, debug=None):
    """Type text at the cursor. Marshals onto the main thread for reliable posting."""
    if not text:
        return
    log = debug or (lambda *a, **k: None)
    try:
        _typer.performSelectorOnMainThread_withObject_waitUntilDone_(
            "typeText:", text, False
        )
        log("typed", chars=len(text))
    except Exception as e:
        log("type_failed", error=repr(e))


