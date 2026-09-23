"""
work_mode.py — Toby Work Mode: working on the screen the way a person would.

In Work Mode Toby doesn't only run commands; it looks at the screen, finds
what it needs, and works on it with a pen, the mouse and the keyboard:
click a button, drag a slider, scroll, circle a number, underline a
sentence, highlight, tick a box, write a note by hand. Each action is
followed by a fresh look, so Toby can check what happened before the next.

    look_at_screen   screenshot -> text with positions -> numbered elements
    click_on         find a target (an element number, words, or x/y) and click
    drag             press on one target, move to another, release
    scroll           turn the wheel over a target (or the page)
    draw             circle / underline / strike / highlight / box / check / cross
                     around or under a target, or an arrow between two
    write_by_hand    write words in handwriting at, below or beside a target

Drawing and handwriting use real pen input (stylus.py): a virtual pressure-
sensitive tablet where available, a mouse drag otherwise. Whether ink
appears depends on the app: in an annotation tool like Kami, pick the pen or
highlighter first (or ask Toby to click it). After drawing, Toby compares the
screen before and after, and says plainly if nothing changed there.

What Toby writes is what you asked it to write. Work Mode annotates and
operates; it isn't a way to have Toby fill in graded school work.
"""

import time

import stylus
import strokes
import vision

SNAPSHOT_FRESH_S = 6.0


class WorkMode:
    def __init__(self, screen_control, windows=lambda: [], settings=dict, cursor=None):
        """cursor(x, y, state, box=None) moves Toby's on-screen pen pointer;
        states: normal, thinking, selecting, drawing, clicking, dragging, done."""
        self.control = screen_control
        self.windows = windows
        self.settings = settings
        self.cursor = cursor or (lambda *a, **k: None)
        self.snapshot = None
        self._pen = None

    # -- looking ----------------------------------------------------------------
    def _size(self):
        return self.control.get_screen_size()

    def look(self):
        """Take a fresh look. Returns the observation text for the model."""
        x, y = self._pointer()
        self.cursor(x, y, "thinking")
        try:
            self.snapshot = vision.look(self._size(), self.windows(),
                                        accessibility=self.settings().get("work_mode_accessibility", True))
        except RuntimeError as e:
            self.cursor(x, y, "normal")
            return f"Couldn't look at the screen: {e}."
        self.cursor(x, y, "normal")
        return vision.describe(self.snapshot)

    def _fresh(self):
        if self.snapshot is None or time.time() - self.snapshot.taken_at > SNAPSHOT_FRESH_S:
            self.look()
        return self.snapshot

    def _pointer(self):
        pos = self.control.pointer_pixels() if hasattr(self.control, "pointer_pixels") else None
        if pos:
            return pos
        w, h = self._size()
        return w / 2, h / 2

    def resolve(self, target=None, x=None, y=None):
        """A box (logical pixels) for what the model pointed at, or raise
        LookupError with a sentence the model can act on."""
        if x is not None and y is not None:
            w, h = self._size()
            px, py = float(x) * w, float(y) * h
            return (px - 4, py - 4, 8, 8)
        if isinstance(target, dict) and "x" in target and "y" in target:
            return self.resolve(x=target["x"], y=target["y"])
        snap = self._fresh()
        if snap is None:
            raise LookupError("Toby couldn't look at the screen.")
        box = vision.find(snap, target)
        if box is None:
            model = self.settings().get("vision_model", "")
            box = vision.locate(snap, str(target), model) if model else None
        if box is None:
            raise LookupError(f"Couldn't find \"{target}\" on the screen. Look again, or give an element number.")
        return box

    # -- acting ---------------------------------------------------------------------
    def _pen_for(self):
        if self._pen is None:
            self._pen = stylus.open_pen(self.control, self.settings().get("work_mode_pen", "auto"))
        return self._pen

    def close(self):
        if self._pen is not None:
            self._pen.close()
            self._pen = None

    def click_on(self, target=None, button="left", double=False, x=None, y=None):
        box = self.resolve(target, x, y)
        cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
        w, h = self._size()
        self.cursor(cx, cy, "selecting", box=box)
        self.control.move(cx / w, cy / h)
        self.cursor(cx, cy, "clicking")
        result = self.control.click(button)
        if double:
            time.sleep(0.08)
            self.control.click(button)
        self.cursor(cx, cy, "normal")
        self.snapshot = None
        what = f"\"{target}\"" if target is not None and not isinstance(target, dict) else "there"
        return f"{'Double-clicked' if double else 'Clicked'} {what}." if result.startswith("Clicked") else result

    def drag(self, source, dest):
        a = self.resolve(source) if not isinstance(source, dict) else self.resolve(x=source["x"], y=source["y"])
        b = self.resolve(dest) if not isinstance(dest, dict) else self.resolve(x=dest["x"], y=dest["y"])
        start = (a[0] + a[2] / 2, a[1] + a[3] / 2)
        end = (b[0] + b[2] / 2, b[1] + b[3] / 2)
        self.control._guard()
        pen = stylus.MousePen(self.control)          # dragging is a mouse gesture everywhere
        stylus.drag(pen, start, end, on_point=lambda px, py, down: self.cursor(px, py, "dragging" if down else "normal"))
        self.snapshot = None
        return "Dragged it across."

    def scroll(self, amount=3, target=None):
        import screen_control as sc
        if target is not None:
            box = self.resolve(target)
            w, h = self._size()
            self.control.move((box[0] + box[2] / 2) / w, (box[1] + box[3] / 2) / h)
        self.control._guard()
        steps = int(max(-30, min(30, int(amount))))
        outcome = sc._run_ydotool(["mousemove", "--wheel", "-x", "0", "-y", str(-steps)], 2)
        if outcome != "ok":
            key = "pagedown" if steps > 0 else "pageup"
            for _ in range(max(1, abs(steps) // 3)):
                sc.press_keys(key)
        self.snapshot = None
        return f"Scrolled {'down' if steps > 0 else 'up'}."

    def draw(self, shape, target=None, to=None, x=None, y=None):
        shape = str(shape or "circle").lower()
        box = self.resolve(target, x, y)
        if shape == "arrow":
            if to is None:
                raise LookupError("An arrow needs somewhere to point: give \"to\" as well.")
            end_box = self.resolve(to) if not isinstance(to, dict) else self.resolve(x=to["x"], y=to["y"])
            start = (box[0] + box[2] / 2, box[1] + box[3] / 2)
            end = (end_box[0] + end_box[2] / 2, end_box[1] + end_box[3] / 2)
            paths = strokes.arrow(start, end)
        else:
            paths = strokes.shape(shape, box)
        return self._ink(paths, f"Drew a {shape}" + (f" around \"{target}\"" if target and shape == "circle" else ""))

    def write_by_hand(self, text, target=None, where="below", size=22, x=None, y=None, font="scripts"):
        text = str(text or "").strip()
        if not text:
            raise LookupError("Say what to write.")
        box = self.resolve(target, x, y) if (target is not None or x is not None) else None
        w, h = self._size()
        size = max(12.0, min(60.0, float(size)))
        if box is None:
            px, py = self._pointer()
        elif where == "right":
            px, py = box[0] + box[2] + 12, box[1] + box[3]
        elif where == "above":
            px, py = box[0], box[1] - size * 0.6
        elif where == "at":
            px, py = box[0], box[1] + box[3]
        else:                                       # below
            px, py = box[0], box[1] + box[3] + size * 1.5
        max_width = max(120.0, w - px - 24)
        paths = strokes.handwriting(text, px, py, height=size, max_width=max_width, font=font)
        return self._ink(paths, f"Wrote \"{text[:40]}\" by hand")

    def _ink(self, paths, summary):
        """Put strokes on the screen with the pen, then check they took."""
        area = strokes.bounds(paths)
        area = (area[0] - 6, area[1] - 6, area[2] + 12, area[3] + 12)
        before = None
        try:
            before, _scale = vision.capture(self._size())
        except RuntimeError:
            before = None
        try:
            pen = self._pen_for()
        except stylus.PenUnavailable as e:
            return f"{summary} failed: {e}."
        stylus.draw(pen, paths, on_point=lambda px, py, down: self.cursor(px, py, "drawing" if down else "normal"))
        time.sleep(0.25)                            # let the app paint the ink
        self.snapshot = None
        if before is None:
            return f"{summary} with the {pen.name}. (Couldn't take screenshots to check it.)"
        try:
            after, scale = vision.capture(self._size())
            changed = vision.region_change(before, after, area, scale)
        except Exception:
            return f"{summary} with the {pen.name}. (Couldn't compare screenshots to check it.)"
        if changed >= 0.004:
            return f"{summary} with the {pen.name}, and the page changed there."
        return (f"{summary} with the {pen.name}, but nothing changed on screen there. The app may need its "
                "pen or highlighter tool selected first, or it may not accept drawing.")


def target_of(action):
    """The target fields of a work-mode action, as the model wrote them."""
    return action.get("target"), action.get("x"), action.get("y")
