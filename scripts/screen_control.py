"""
screen_control.py — mouse/keyboard automation with an explicit, code-enforced
consent gate, and animated ("gliding") mouse movement instead of instant jumps.
"""

import subprocess

from gi.repository import GLib

_VALID_BUTTONS = {"left", "right", "middle"}
_GLIDE_STEPS = 24
_GLIDE_INTERVAL_MS = 12  # ~24 steps * 12ms ≈ 290ms glide duration


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
        step_count = [0]

        def ease_out_cubic(t):
            return 1 - (1 - t) ** 3

        def step():
            step_count[0] += 1
            t = min(1.0, step_count[0] / _GLIDE_STEPS)
            eased = ease_out_cubic(t)
            cur_x = start_x + (end_x - start_x) * eased
            cur_y = start_y + (end_y - start_y) * eased
            px, py = self._to_pixels(cur_x, cur_y)
            try:
                subprocess.run(["ydotool", "mousemove", "--absolute", str(px), str(py)],
                                check=False, timeout=1.5)
            except subprocess.TimeoutExpired:
                pass  # ydotoold likely isn't running — don't hang the glide loop over it
            if t >= 1.0:
                self.current_x_frac, self.current_y_frac = end_x, end_y
                return False  # stop the timeout
            return True  # keep going

        GLib.timeout_add(_GLIDE_INTERVAL_MS, step)
        px, py = self._to_pixels(end_x, end_y)
        return f"Gliding mouse to ({px}, {py})"

    def click(self, button="left"):
        self._guard()
        if button not in _VALID_BUTTONS:
            button = "left"
        try:
            subprocess.run(["ydotool", "click", button], check=False, timeout=2)
        except subprocess.TimeoutExpired:
            return "Click timed out — is ydotoold running? (systemctl --user status ydotool)"
        return f"Clicked {button}"

    def type_text(self, text):
        self._guard()
        try:
            subprocess.run(["ydotool", "type", text], check=False, timeout=5)
        except subprocess.TimeoutExpired:
            return "Typing timed out — is ydotoold running? (systemctl --user status ydotool)"
        return f"Typed: {text[:40]}"

    def key(self, keys):
        self._guard()
        try:
            subprocess.run(["ydotool", "key", keys], check=False, timeout=2)
        except subprocess.TimeoutExpired:
            return "Key press timed out — is ydotoold running? (systemctl --user status ydotool)"
        return f"Pressed: {keys}"
