"""Floating notch overlay pill showing dictation state (Listening/Transcribing/Done).

A borderless, always-on-top, click-through translucent window anchored top-center
under the menu bar / notch. This replaces the menu bar title as the "listening"
indicator because the menu bar is peripheral, gets truncated, and competes with
the OS microphone indicator. The pill sits where the user's eyes are.

All AppKit window mutation must happen on the main thread; callers may invoke from
the hotkey tap thread, so show/hide/set_state bounce onto the main thread via an
NSObject helper (same pattern used for keystroke injection).
"""
import objc
from AppKit import (
    NSApplication, NSBackingStoreBuffered, NSColor, NSFont,
    NSFloatingWindowLevel, NSScreen, NSTextField, NSView, NSWindow,
    NSWindowStyleMaskBorderless,
)
from Foundation import NSMakeRect, NSObject, NSTimer

_STATE_TEXT = {
    "listening": "🔴  Listening…",
    "transcribing": "⏳  Transcribing…",
    "done": "✓  Done",
    "auth_needed": "🔑  Auth needed: refresh AWS creds",
    "error": "⚠️  Transcribe timed out",
}

_PILL_W = 200.0
_PILL_W_WIDE = 320.0
_PILL_H = 44.0
_TOP_GAP = 8.0


class _OverlayImpl(NSObject):
    def init(self):
        self = objc.super(_OverlayImpl, self).init()
        if self is None:
            return None
        self._window = None
        self._label = None
        self._content = None
        self._hide_timer = None
        return self

    def _ensure_window(self):
        if self._window is not None:
            return
        rect = NSMakeRect(0, 0, _PILL_W, _PILL_H)

        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False
        )
        win.setLevel_(NSFloatingWindowLevel)
        win.setOpaque_(False)
        win.setBackgroundColor_(NSColor.clearColor())
        win.setIgnoresMouseEvents_(True)
        win.setHasShadow_(True)

        content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, _PILL_W, _PILL_H))
        content.setWantsLayer_(True)
        layer = content.layer()
        layer.setCornerRadius_(_PILL_H / 2.0)
        layer.setBackgroundColor_(
            NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.82).CGColor()
        )
        win.setContentView_(content)

        label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(0, 0, _PILL_W, _PILL_H)
        )
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setAlignment_(1)  # center
        label.setTextColor_(NSColor.whiteColor())
        label.setFont_(NSFont.systemFontOfSize_weight_(15.0, 0.3))
        label.setFrame_(NSMakeRect(0, (_PILL_H - 22) / 2.0, _PILL_W, 22))
        content.addSubview_(label)
        self._content = label.superview()
        self._label = label
        self._window = win

    def _resize_to(self, width):
        screen = NSScreen.mainScreen()
        frame = screen.frame()
        x = frame.origin.x + (frame.size.width - width) / 2.0
        y = frame.origin.y + frame.size.height - _PILL_H - _TOP_GAP
        self._window.setFrame_display_(NSMakeRect(x, y, width, _PILL_H), True)
        self._content.setFrame_(NSMakeRect(0, 0, width, _PILL_H))
        self._label.setFrame_(NSMakeRect(0, (_PILL_H - 22) / 2.0, width, 22))

    def showState_(self, state):
        self._ensure_window()
        if self._hide_timer is not None:
            self._hide_timer.invalidate()
            self._hide_timer = None
        self._resize_to(_PILL_W)
        self._label.setStringValue_(_STATE_TEXT.get(state, ""))
        self._window.orderFrontRegardless()

    def showAuthNeeded(self):
        self._ensure_window()
        if self._hide_timer is not None:
            self._hide_timer.invalidate()
            self._hide_timer = None
        self._resize_to(_PILL_W_WIDE)
        self._label.setStringValue_(_STATE_TEXT["auth_needed"])
        self._window.orderFrontRegardless()
        self._hide_timer = (
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                4.0, self, "hideNow:", None, False
            )
        )

    def showDoneThenHide(self):
        self._ensure_window()
        self._label.setStringValue_(_STATE_TEXT["done"])
        self._window.orderFrontRegardless()
        if self._hide_timer is not None:
            self._hide_timer.invalidate()
        self._hide_timer = (
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                0.9, self, "hideNow:", None, False
            )
        )

    def showErrorThenHide(self):
        self._ensure_window()
        self._resize_to(_PILL_W_WIDE)
        self._label.setStringValue_(_STATE_TEXT["error"])
        self._window.orderFrontRegardless()
        if self._hide_timer is not None:
            self._hide_timer.invalidate()
        self._hide_timer = (
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                2.5, self, "hideNow:", None, False
            )
        )

    def hideNow_(self, _timer):
        self._hide_timer = None
        if self._window is not None:
            self._window.orderOut_(None)

    def hide(self):
        if self._hide_timer is not None:
            self._hide_timer.invalidate()
            self._hide_timer = None
        if self._window is not None:
            self._window.orderOut_(None)


class Overlay:
    """Main-thread-safe facade. Call show_state / done / hide from any thread."""

    def __init__(self):
        self._impl = _OverlayImpl.alloc().init()

    def show_state(self, state):
        self._impl.performSelectorOnMainThread_withObject_waitUntilDone_(
            "showState:", state, False
        )

    def done(self):
        self._impl.performSelectorOnMainThread_withObject_waitUntilDone_(
            "showDoneThenHide", None, False
        )

    def error(self):
        self._impl.performSelectorOnMainThread_withObject_waitUntilDone_(
            "showErrorThenHide", None, False
        )

    def auth_needed(self):
        self._impl.performSelectorOnMainThread_withObject_waitUntilDone_(
            "showAuthNeeded", None, False
        )

    def hide(self):
        self._impl.performSelectorOnMainThread_withObject_waitUntilDone_(
            "hide", None, False
        )
