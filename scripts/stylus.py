"""
stylus.py — Toby's pen: real input events, not an animation on top.

Two ways to put a stroke on the screen:

  tablet   A virtual pen tablet, created through /dev/uinput with python-evdev.
           It reports pen proximity, touch and pressure, so apps that
           understand pens (browsers with Pointer Events, drawing apps,
           annotation tools like Kami) see a real stylus with pressure.
  mouse    Hold the left button down and move the pointer, through ydotool,
           the same way Toby already moves the mouse. Every app that lets
           you draw with a mouse accepts this.

"auto" (the default) uses the tablet when python-evdev can open /dev/uinput
and there's a single monitor (a tablet maps onto the whole desktop, which
is only unambiguous with one screen); otherwise the mouse.

Nothing here decides whether Toby may draw: the task runner asks for the
same mouse-and-keyboard permission first, and every pen here refuses to
move unless screen_control has been granted.
"""

import os
import time

import strokes as strokes_mod

TABLET_MAX = 32767
PRESSURE_MAX = 4095


class PenUnavailable(Exception):
    pass


class Pen:
    """Moves to points and touches down / lifts up. Screen pixel coordinates."""
    name = "pen"

    def hover(self, x, y):
        raise NotImplementedError

    def down(self, x, y, pressure=0.8):
        raise NotImplementedError

    def move(self, x, y, pressure=0.8):
        raise NotImplementedError

    def up(self, x, y):
        raise NotImplementedError

    def close(self):
        pass


class MousePen(Pen):
    """Left button held down while the pointer moves, via ydotool."""
    name = "mouse"

    def __init__(self, screen_control, runner=None):
        import screen_control as sc
        self.control = screen_control
        self.sc = sc
        self.run = runner or sc._run_ydotool

    def _move(self, x, y):
        self.control._guard()
        outcome = self.sc.move_pointer(int(round(x)), int(round(y)))
        if outcome != "ok":
            raise PenUnavailable("ydotool couldn't move the pointer; is ydotoold running?")
        w, h = self.control.get_screen_size()
        self.control.current_x_frac, self.control.current_y_frac = x / w, y / h

    def hover(self, x, y):
        self._move(x, y)

    def down(self, x, y, pressure=0.8):
        self._move(x, y)
        if self.run(["click", "0x40"], 2) != "ok":      # left button down only
            raise PenUnavailable("this ydotool can't hold the button down (it needs ydotool 1.0)")

    def move(self, x, y, pressure=0.8):
        self._move(x, y)

    def up(self, x, y):
        self._move(x, y)
        self.run(["click", "0x80"], 2)                  # left button up only


class TabletPen(Pen):
    """A virtual pen tablet with pressure, through /dev/uinput."""
    name = "tablet"

    def __init__(self, screen_control, width, height, evdev_module=None):
        try:
            evdev = evdev_module or __import__("evdev")
        except ImportError as e:
            raise PenUnavailable("python-evdev isn't installed") from e
        self.control = screen_control
        self.width, self.height = float(width), float(height)
        self.e = evdev.ecodes
        caps = {
            self.e.EV_KEY: [self.e.BTN_TOOL_PEN, self.e.BTN_TOUCH, self.e.BTN_STYLUS],
            self.e.EV_ABS: [
                (self.e.ABS_X, evdev.AbsInfo(0, 0, TABLET_MAX, 0, 0, 100)),
                (self.e.ABS_Y, evdev.AbsInfo(0, 0, TABLET_MAX, 0, 0, 100)),
                (self.e.ABS_PRESSURE, evdev.AbsInfo(0, 0, PRESSURE_MAX, 0, 0, 0)),
            ],
        }
        try:
            self.device = evdev.UInput(caps, name="Little Toby pen", input_props=[self.e.INPUT_PROP_DIRECT])
        except (OSError, PermissionError) as e:
            raise PenUnavailable(f"can't create a virtual pen: {e}") from e
        time.sleep(0.3)   # let the compositor notice the new device
        self.near = False

    def _abs(self, x, y):
        return (int(max(0, min(1, x / self.width)) * TABLET_MAX),
                int(max(0, min(1, y / self.height)) * TABLET_MAX))

    def _emit(self, x, y, pressure=None, keys=()):
        self.control._guard()
        ax, ay = self._abs(x, y)
        for code, value in keys:
            self.device.write(self.e.EV_KEY, code, value)
        self.device.write(self.e.EV_ABS, self.e.ABS_X, ax)
        self.device.write(self.e.EV_ABS, self.e.ABS_Y, ay)
        if pressure is not None:
            self.device.write(self.e.EV_ABS, self.e.ABS_PRESSURE, int(pressure * PRESSURE_MAX))
        self.device.syn()
        self.control.current_x_frac, self.control.current_y_frac = x / self.width, y / self.height

    def hover(self, x, y):
        keys = () if self.near else ((self.e.BTN_TOOL_PEN, 1),)
        self.near = True
        self._emit(x, y, 0, keys)

    def down(self, x, y, pressure=0.8):
        if not self.near:
            self.hover(x, y)
        self._emit(x, y, pressure, ((self.e.BTN_TOUCH, 1),))

    def move(self, x, y, pressure=0.8):
        self._emit(x, y, pressure)

    def up(self, x, y):
        self._emit(x, y, 0, ((self.e.BTN_TOUCH, 0),))

    def close(self):
        if self.near:
            try:
                self._emit(*self._last_or_center(), 0, ((self.e.BTN_TOOL_PEN, 0),))
            except Exception:
                pass
        try:
            self.device.close()
        except Exception:
            pass

    def _last_or_center(self):
        return (self.control.current_x_frac * self.width, self.control.current_y_frac * self.height)


def monitor_count():
    import json
    import subprocess
    try:
        out = subprocess.run(["hyprctl", "-j", "monitors"], capture_output=True, text=True, timeout=2)
        return len(json.loads(out.stdout or "[]"))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0


def open_pen(screen_control, preference="auto"):
    """The best pen available. Raises PenUnavailable only if none works."""
    w, h = screen_control.get_screen_size()
    if preference in ("auto", "tablet"):
        single = monitor_count() <= 1
        if preference == "tablet" or single:
            try:
                if os.access("/dev/uinput", os.W_OK):
                    return TabletPen(screen_control, w, h)
            except PenUnavailable:
                if preference == "tablet":
                    raise
    return MousePen(screen_control)


def draw(pen, strokes, speed=520.0, spacing=4.0, sleep=time.sleep, on_point=None):
    """Play strokes through a pen at about `speed` pixels a second, lifting
    the pen between strokes. on_point(x, y, touching) is called for every
    point, so the on-screen pen cursor can follow."""
    dt = spacing / max(50.0, speed)
    for stroke in strokes:
        points = strokes_mod.resample(stroke, spacing)
        if not points:
            continue
        x, y = points[0]
        pen.hover(x, y)
        if on_point:
            on_point(x, y, False)
        pen.down(x, y, strokes_mod.pressure(0))
        n = max(1, len(points) - 1)
        for i, (x, y) in enumerate(points[1:], start=1):
            pen.move(x, y, strokes_mod.pressure(i / n))
            if on_point:
                on_point(x, y, True)
            sleep(dt)
        pen.up(x, y)
        if on_point:
            on_point(x, y, False)
        sleep(0.03)


def drag(pen, start, end, sleep=time.sleep, on_point=None):
    """Press at start, move to end, release: moving a thing, selecting text."""
    draw(pen, [strokes_mod.line(start, end, spacing=8)], speed=700, spacing=8, sleep=sleep, on_point=on_point)
