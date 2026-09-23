"""
screen_control.py — mouse/keyboard automation with an explicit, code-enforced
consent gate, and animated ("gliding") mouse movement instead of instant jumps.
"""

import subprocess
import time

from gi.repository import GLib

import toby_anim

_VALID_BUTTONS = {"left", "right", "middle"}
_GLIDE_SECONDS = 0.34   # toby_anim.DURATIONS["emphasized"]
_GLIDE_INTERVAL_MS = 12

# ---------------------------------------------------------------------------
# ydotool syntax compatibility
#
# ydotool 1.0 changed both of the commands this file depends on, and the old
# spellings do not error loudly — they print usage text and exit non-zero,
# which with check=False looks exactly like success. So "Pressed: ctrl+w"
# could be reported for a key press that never happened.
#
#   key:    "ydotool key ctrl+w"  ->  "ydotool key 29:1 17:1 17:0 29:0"
#           (keycodes from linux/input-event-codes.h, :1 down, :0 up)
#   click:  "ydotool click left"  ->  "ydotool click 0xC0"
#           (0x40 is the down bit, 0x80 the up bit, low bits the button)
#
# Both helpers below try the current syntax first and fall back to the older
# one if ydotool rejects it, so either version works without configuration.
# ---------------------------------------------------------------------------

_KEYCODES = {
    "esc": 1, "escape": 1,
    "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
    "minus": 12, "-": 12, "equal": 13, "=": 13,
    "backspace": 14, "tab": 15,
    "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22, "i": 23, "o": 24, "p": 25,
    "leftbrace": 26, "[": 26, "rightbrace": 27, "]": 27,
    "enter": 28, "return": 28,
    "ctrl": 29, "leftctrl": 29, "control": 29,
    "a": 30, "s": 31, "d": 32, "f": 33, "g": 34, "h": 35, "j": 36, "k": 37, "l": 38,
    "semicolon": 39, ";": 39, "apostrophe": 40, "'": 40, "grave": 41, "`": 41,
    "shift": 42, "leftshift": 42,
    "backslash": 43, "\\": 43,
    "z": 44, "x": 45, "c": 46, "v": 47, "b": 48, "n": 49, "m": 50,
    "comma": 51, ",": 51, "dot": 52, "period": 52, ".": 52, "slash": 53, "/": 53,
    "rightshift": 54,
    "alt": 56, "leftalt": 56,
    "space": 57, "capslock": 58,
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64, "f7": 65, "f8": 66,
    "f9": 67, "f10": 68, "f11": 87, "f12": 88,
    "rightctrl": 97, "rightalt": 100, "altgr": 100,
    "home": 102, "up": 103, "pageup": 104, "pgup": 104,
    "left": 105, "right": 106, "end": 107, "down": 108,
    "pagedown": 109, "pgdn": 109, "insert": 110, "ins": 110, "delete": 111, "del": 111,
    "super": 125, "meta": 125, "win": 125, "cmd": 125, "leftmeta": 125,
    "rightmeta": 126, "rightsuper": 126,
    "print": 99, "printscreen": 99, "sysrq": 99,
}

_BUTTON_CODES = {"left": "0xC0", "right": "0xC1", "middle": "0xC2"}
_LEGACY_BUTTON_CODES = {"left": "0", "right": "1", "middle": "2"}


def keycode_sequence(combo):
    """Turn "ctrl+shift+t" into ydotool 1.0 keycode arguments.

    Returns None if any part of the combo isn't a key we know a code for,
    so the caller can fall back rather than press something wrong.
    """
    parts = [p.strip().lower() for p in str(combo).split("+") if p.strip()]
    if not parts:
        return None
    codes = []
    for part in parts:
        code = _KEYCODES.get(part)
        if code is None:
            return None
        codes.append(code)
    # press modifiers then the key, release in the reverse order
    return [f"{c}:1" for c in codes] + [f"{c}:0" for c in reversed(codes)]


_YDOTOOL_MISSING = ("ydotool isn't installed, so Toby can't drive the mouse or "
                    "keyboard. Install it and enable ydotoold: "
                    "systemctl --user enable --now ydotool.service")
_YDOTOOL_TIMEOUT = ("That timed out — ydotoold probably isn't running. Start it with: "
                    "systemctl --user enable --now ydotool.service")


def _run_ydotool(args, timeout):
    """Run one ydotool command.

    Returns "ok", "rejected" (ydotool ran but didn't like the arguments),
    "timeout" (almost always a missing ydotoold, which makes ydotool hang
    rather than fail) or "missing" (no ydotool binary at all).
    """
    try:
        result = subprocess.run(["ydotool"] + args, capture_output=True,
                                text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "timeout"
    except FileNotFoundError:
        return "missing"
    return "ok" if result.returncode == 0 else "rejected"


def press_keys(combo, timeout=2):
    """Press a key combination, coping with either ydotool syntax.

    Returns a short human-readable result string, the same shape the rest of
    the tool dispatch table returns.
    """
    sequence = keycode_sequence(combo)
    if sequence:
        outcome = _run_ydotool(["key"] + sequence, timeout)
        if outcome == "ok":
            return f"Pressed: {combo}"
        if outcome in ("timeout", "missing"):
            return _YDOTOOL_TIMEOUT if outcome == "timeout" else _YDOTOOL_MISSING
    # Either the combo used a key we have no code for, or this is an older
    # ydotool that still wants the name form.
    outcome = _run_ydotool(["key", str(combo)], timeout)
    if outcome == "ok":
        return f"Pressed: {combo}"
    if outcome == "timeout":
        return _YDOTOOL_TIMEOUT
    if outcome == "missing":
        return _YDOTOOL_MISSING
    if not sequence:
        return f"Don't know how to press '{combo}'."
    return f"ydotool wouldn't accept the key press '{combo}'."


def press_button(button, timeout=2):
    """Click a mouse button, coping with either ydotool syntax."""
    if button not in _VALID_BUTTONS:
        button = "left"
    for code in (_BUTTON_CODES[button], _LEGACY_BUTTON_CODES[button]):
        outcome = _run_ydotool(["click", code], timeout)
        if outcome == "ok":
            return f"Clicked {button}"
        if outcome == "timeout":
            return _YDOTOOL_TIMEOUT
        if outcome == "missing":
            return _YDOTOOL_MISSING
    return f"ydotool wouldn't accept a {button} click."


def move_pointer(px, py, timeout=1.5):
    """Move the pointer to absolute screen pixels. Returns an outcome string."""
    return _run_ydotool(["mousemove", "--absolute", str(px), str(py)], timeout)


class NeedsConfirmation(Exception):
    pass


class ScreenControl:
    def __init__(self):
        self.enabled = False
        self.get_screen_size = None
        # Tracked internally since Wayland has no standard "get cursor pos" call.
        self.current_x_frac = 0.5
        self.current_y_frac = 0.5

    def grant(self):
        self.enabled = True

    def deny(self):
        self.enabled = False

    def disable(self, *_args):
        self.enabled = False
        return "Screen control disabled."

    def emergency_stop(self):
        self.enabled = False

    def _guard(self):
        if not self.enabled:
            raise NeedsConfirmation()

    def _to_pixels(self, x_frac, y_frac):
        if self.get_screen_size is None:
            raise RuntimeError("get_screen_size not wired up.")
        w, h = self.get_screen_size()
        x_frac = max(0.0, min(1.0, float(x_frac)))
        y_frac = max(0.0, min(1.0, float(y_frac)))
        return int(x_frac * w), int(y_frac * h)

    def move(self, x_frac, y_frac):
        self._guard()
        x_frac = max(0.0, min(1.0, float(x_frac)))
        y_frac = max(0.0, min(1.0, float(y_frac)))

        start_x, start_y = self.current_x_frac, self.current_y_frac
        end_x, end_y = x_frac, y_frac
        t0 = time.monotonic()
        ease = toby_anim.EASE["move"]

        def step():
            # Time-based on the shared "move" curve: a busy machine makes the
            # glide choppier, never slower. (It used to count 24 fixed steps.)
            t = min(1.0, (time.monotonic() - t0) / _GLIDE_SECONDS)
            eased = ease(t)
            cur_x = start_x + (end_x - start_x) * eased
            cur_y = start_y + (end_y - start_y) * eased
            px, py = self._to_pixels(cur_x, cur_y)
            # A missing ydotoold makes every call hang until its timeout, which
            # would drag a glide out for many seconds. Give up on the whole
            # glide the first time a step doesn't land.
            if move_pointer(px, py) != "ok":
                self.current_x_frac, self.current_y_frac = end_x, end_y
                return False
            if t >= 1.0:
                self.current_x_frac, self.current_y_frac = end_x, end_y
                return False  # stop the timeout
            return True  # keep going

        GLib.timeout_add(_GLIDE_INTERVAL_MS, step)
        px, py = self._to_pixels(end_x, end_y)
        return f"Gliding mouse to ({px}, {py})"

    def move_immediate(self, x_frac, y_frac):
        """Jump the pointer straight to a position, with no glide.

        move() eases the pointer over about a third of a second, which is
        right when Toby is doing something on the user's behalf and wrong
        when the pointer is following a hand — there, every frame is a new
        target and the easing just adds lag. Same consent gate either way.
        """
        self._guard()
        x_frac = max(0.0, min(1.0, float(x_frac)))
        y_frac = max(0.0, min(1.0, float(y_frac)))
        px, py = self._to_pixels(x_frac, y_frac)
        outcome = move_pointer(px, py)
        if outcome == "ok":
            self.current_x_frac, self.current_y_frac = x_frac, y_frac
        return outcome

    def click(self, button="left"):
        self._guard()
        return press_button(button)

    def type_text(self, text):
        self._guard()
        outcome = _run_ydotool(["type", text], 5)
        if outcome == "timeout":
            return _YDOTOOL_TIMEOUT
        if outcome == "missing":
            return _YDOTOOL_MISSING
        if outcome != "ok":
            return "ydotool wouldn't type that."
        return f"Typed: {text[:40]}"

    def key(self, keys):
        self._guard()
        return press_keys(keys)
