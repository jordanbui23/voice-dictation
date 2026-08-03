"""Global hold-to-talk hotkey via CGEventTap (pyobjc). NOT pynput.

Right-side modifiers report a device-specific flag bit in addition to the generic
modifier mask, so we can distinguish Right Command from Left Command. For a plain
key (f5) we watch keyDown/keyUp instead.

The tap runs on its own CFRunLoop thread. Callbacks fire on that thread; the app
marshals real work onto a worker thread so we never block the tap (which would make
the whole system's input laggy).
"""
import threading
import time

import Quartz

# Device-dependent flag bits for individual modifier keys (IOKit values).
_RIGHT_CMD_MASK = 0x000010
_RIGHT_OPTION_MASK = 0x000040
_RIGHT_CONTROL_MASK = 0x002000

_MODIFIER_HOTKEYS = {
    "right_cmd": (Quartz.kCGEventFlagMaskCommand, _RIGHT_CMD_MASK),
    "right_option": (Quartz.kCGEventFlagMaskAlternate, _RIGHT_OPTION_MASK),
    "right_control": (Quartz.kCGEventFlagMaskControl, _RIGHT_CONTROL_MASK),
}

# Combos require ALL listed modifiers held together; release fires when any lifts.
_MODIFIER_COMBOS = {
    "right_cmd+right_option": ["right_cmd", "right_option"],
    "right_cmd+right_control": ["right_cmd", "right_control"],
    "right_option+right_control": ["right_option", "right_control"],
}

_KEYCODES = {"f5": 96}


def _combo_pressed(flags, parts):
    for part in parts:
        generic_mask, device_mask = _MODIFIER_HOTKEYS[part]
        if not (flags & generic_mask and flags & device_mask):
            return False
    return True


def _generic_masks(hotkey):
    """Generic modifier masks a hotkey uses (left/right agnostic).

    CGEventSourceFlagsState carries only generic masks, not the device-specific
    right-side bits, so the stuck-release poll compares against these.
    """
    if hotkey in _MODIFIER_COMBOS:
        parts = _MODIFIER_COMBOS[hotkey]
    elif hotkey in _MODIFIER_HOTKEYS:
        parts = [hotkey]
    else:
        return []
    return [_MODIFIER_HOTKEYS[p][0] for p in parts]


class HotkeyListener:
    def __init__(self, hotkey, on_press, on_release):
        self.hotkey = hotkey
        self.on_press = on_press
        self.on_release = on_release
        self._down = False
        self._tap = None
        self._runloop_source = None
        self._loop = None
        self._thread = None
        self._generic_masks = _generic_masks(hotkey)
        self._press_time = 0.0
        self._watchdog = None
        self._watchdog_stop = threading.Event()

    def _handle(self, proxy, etype, event, refcon):
        if etype in (Quartz.kCGEventTapDisabledByTimeout,
                     Quartz.kCGEventTapDisabledByUserInput):
            if self._tap is not None:
                Quartz.CGEventTapEnable(self._tap, True)
            return event
        try:
            if self.hotkey in _MODIFIER_COMBOS:
                flags = Quartz.CGEventGetFlags(event)
                pressed = _combo_pressed(flags, _MODIFIER_COMBOS[self.hotkey])
                if pressed and not self._down:
                    self._down = True
                    self._press_time = time.monotonic()
                    self.on_press()
                elif not pressed and self._down:
                    self._down = False
                    self.on_release()
            elif self.hotkey in _MODIFIER_HOTKEYS:
                generic_mask, device_mask = _MODIFIER_HOTKEYS[self.hotkey]
                flags = Quartz.CGEventGetFlags(event)
                pressed = bool(flags & generic_mask) and bool(flags & device_mask)
                if pressed and not self._down:
                    self._down = True
                    self._press_time = time.monotonic()
                    self.on_press()
                elif not pressed and self._down:
                    self._down = False
                    self.on_release()
            else:
                keycode = Quartz.CGEventGetIntegerValueField(
                    event, Quartz.kCGKeyboardEventKeycode
                )
                if keycode == _KEYCODES.get(self.hotkey):
                    if etype == Quartz.kCGEventKeyDown and not self._down:
                        self._down = True
                        self._press_time = time.monotonic()
                        self.on_press()
                    elif etype == Quartz.kCGEventKeyUp and self._down:
                        self._down = False
                        self.on_release()
                    return None  # consume the hotkey so it never reaches the app
        except Exception:
            pass
        return event

    def _watch(self):
        """Recover a stuck press when the release event was lost.

        CGEventSourceFlagsState only carries generic modifier masks, so this
        checks whether ALL of the hotkey's generic modifiers have gone absent.
        That can only ever recover a genuinely stuck state, never interrupt an
        active hold, so there is no cap on recording length. Requires two
        consecutive absent readings and >=1s since press to avoid a startup race.
        """
        absent_streak = 0
        while not self._watchdog_stop.wait(0.15):
            if not self._down:
                absent_streak = 0
                continue
            if time.monotonic() - self._press_time < 1.0:
                absent_streak = 0
                continue
            flags = Quartz.CGEventSourceFlagsState(
                Quartz.kCGEventSourceStateCombinedSessionState
            )
            all_absent = all(not (flags & m) for m in self._generic_masks)
            if not all_absent:
                absent_streak = 0
                continue
            absent_streak += 1
            if absent_streak >= 2 and self._down:
                self._down = False
                absent_streak = 0
                try:
                    self.on_release()
                except Exception:
                    pass

    def _run(self):
        if self.hotkey in _MODIFIER_COMBOS or self.hotkey in _MODIFIER_HOTKEYS:
            mask = Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged)
            tap_option = Quartz.kCGEventTapOptionListenOnly
        else:
            mask = (Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
                    | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp))
            tap_option = Quartz.kCGEventTapOptionDefault

        self._tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            tap_option,
            mask,
            self._handle,
            None,
        )
        if self._tap is None:
            raise RuntimeError(
                "Failed to create event tap. Grant Accessibility + Input "
                "Monitoring permission to the launching app (Terminal/OpenCode)."
            )

        self._runloop_source = Quartz.CFMachPortCreateRunLoopSource(
            None, self._tap, 0
        )
        self._loop = Quartz.CFRunLoopGetCurrent()
        Quartz.CFRunLoopAddSource(
            self._loop, self._runloop_source, Quartz.kCFRunLoopCommonModes
        )
        Quartz.CGEventTapEnable(self._tap, True)
        Quartz.CFRunLoopRun()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if self._generic_masks:
            self._watchdog_stop.clear()
            self._watchdog = threading.Thread(target=self._watch, daemon=True)
            self._watchdog.start()

    def stop(self):
        self._watchdog_stop.set()
        if self._loop is not None:
            Quartz.CFRunLoopStop(self._loop)
