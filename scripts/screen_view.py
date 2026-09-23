"""
screen_view.py — what your phone sees when you look at the computer.

Two views, both real screenshots taken with grim at the moment you ask:

  full   the whole screen, scaled down to fit a phone
  toby   just the window Toby is working in (the focused one), so a
         dialog or a document is legible on a phone without zooming

It is off unless you turn on "Let paired phones see the screen" on the
computer; the phone can't turn it on. While a phone is looking, Toby shows
it on the computer, so a screen being watched never goes unnoticed.

Captures are JPEG at moderate quality and at most a few per second no matter
how many phones ask, which keeps it light on an integrated-graphics laptop
and on your phone's data.
"""

import json
import shutil
import subprocess
import threading
import time

MIN_INTERVAL_S = 0.3
JPEG_QUALITY = 60


class ScreenViewer:
    def __init__(self, runner=subprocess.run, clock=time.monotonic):
        self.runner = runner
        self.clock = clock
        self.last_viewed = 0.0
        self._cache = {}
        self._lock = threading.Lock()

    def viewing(self, within=4.0):
        """Whether a phone has looked at the screen in the last few seconds."""
        return self.last_viewed and self.clock() - self.last_viewed < within

    def _json(self, args):
        try:
            out = self.runner(["hyprctl", "-j"] + args, capture_output=True, text=True, timeout=2)
            return json.loads(out.stdout or "null")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None

    def _region(self, view):
        """(geometry for grim or None, width in pixels or None)."""
        if view == "toby":
            win = self._json(["activewindow"])
            if isinstance(win, dict) and win.get("size") and win.get("at"):
                (x, y), (w, h) = win["at"], win["size"]
                if w > 0 and h > 0:
                    return f"{x},{y} {w}x{h}", w
        monitors = self._json(["monitors"])
        if isinstance(monitors, list) and monitors:
            focused = next((m for m in monitors if m.get("focused")), monitors[0])
            scale = focused.get("scale") or 1
            return None, int(focused.get("width", 0) / scale) or None
        return None, None

    def capture(self, view="full", max_width=1170):
        """(jpeg bytes, None) or (None, reason)."""
        if not shutil.which("grim"):
            return None, "grim_missing"
        key = (view, max_width)
        with self._lock:
            self.last_viewed = self.clock()
            cached = self._cache.get(key)
            if cached and self.clock() - cached[0] < MIN_INTERVAL_S:
                return cached[1], None
            geometry, width = self._region(view)
            scale = min(1.0, max_width / width) if width else 0.5
            args = ["grim", "-t", "jpeg", "-q", str(JPEG_QUALITY), "-s", f"{scale:.3f}"]
            if geometry:
                args += ["-g", geometry]
            args.append("-")
            try:
                out = self.runner(args, capture_output=True, timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                return None, "capture_failed"
            if out.returncode != 0 or not out.stdout:
                return None, "capture_failed"
            self._cache = {key: (self.clock(), out.stdout)}
            return out.stdout, None
