"""Work Mode: strokes, the pen, seeing the screen, and checking the result.

The "screen" here is a page rendered with Cairo: a small worksheet with a
total, a sentence and two buttons. Tesseract really reads it (the check is
skipped if tesseract isn't installed). A stand-in drawing app paints ink on
that page wherever the pen touches down and moves, so Toby's whole loop is
exercised: look, find "42.50" by its text, circle it with real pen events,
take another screenshot, and see that the page changed there. A second app
that ignores the pen checks Toby says so instead of claiming success.
"""
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import cairo  # noqa: E402

import strokes  # noqa: E402
import stylus  # noqa: E402
import vision  # noqa: E402
import work_mode  # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def check_true(label, got):
    if not got:
        failures.append(f"{label}: expected true, got {got!r}")


# -- strokes --------------------------------------------------------------------------------
font = strokes.load_font("scripts")
check("the handwriting font has every printable character", len(font["glyphs"]), 96)
written = strokes.handwriting("Hello there", 100, 200, height=30)
x, y, w, h = strokes.bounds(written)
check_true("handwriting sits on its baseline", 200 - 32 <= y <= 200 and y + h >= 195)
check_true("capitals are about the size asked for", 24 <= h <= 60)
check_true("it's made of separate pen strokes", len(written) >= 8)
wrapped = strokes.handwriting("one two three four five six seven eight", 0, 50, height=20, max_width=150)
check_true("long text wraps within the width", strokes.bounds(wrapped)[2] <= 160)
check_true("onto more lines", strokes.bounds(wrapped)[3] > 40)
printed = strokes.handwriting("Label", 0, 40, height=20, font="futural")
check_true("a print hand is available too", len(printed) > 3)

box = (100, 100, 60, 20)
loop = strokes.circle_around(box)[0]
inside = [p for p in loop if 100 < p[0] < 160 and 100 < p[1] < 120]
check("a circle goes round, not through, what it circles", inside, [])
check_true("and overlaps where it started, like a hand-drawn loop",
           abs(loop[0][0] - loop[-1][0]) < 30 and abs(loop[0][1] - loop[-1][1]) < 30)
under = strokes.underline(box)[0]
check_true("an underline is just below", all(123 <= p[1] <= 127 for p in under))
check_true("and spans the width", under[0][0] <= 100 and under[-1][0] >= 160)
check("an arrow is a shaft and two wings", len(strokes.arrow((0, 0), (100, 0))), 3)
try:
    strokes.shape("spiral", box)
    failures.append("an unknown shape is refused")
except ValueError:
    pass
even = strokes.resample([(0, 0), (100, 0)], spacing=10)
check("strokes are resampled evenly", len(even), 11)
check_true("pen pressure rises, holds and lifts",
           strokes.pressure(0) < strokes.pressure(0.5) and strokes.pressure(1) < strokes.pressure(0.5))

# -- the pens --------------------------------------------------------------------------------
class Guard:
    def __init__(self):
        self.enabled = True
        self.current_x_frac = self.current_y_frac = 0.5

    def _guard(self):
        if not self.enabled:
            raise PermissionError("not allowed")

    def get_screen_size(self):
        return (1000, 800)


class RecordingPen(stylus.Pen):
    name = "test pen"

    def __init__(self):
        self.events = []

    def hover(self, x, y): self.events.append(("hover", round(x), round(y)))
    def down(self, x, y, pressure=0.8): self.events.append(("down", round(x), round(y)))
    def move(self, x, y, pressure=0.8): self.events.append(("move", round(x), round(y), round(pressure, 2)))
    def up(self, x, y): self.events.append(("up", round(x), round(y)))


pen = RecordingPen()
seen = []
stylus.draw(pen, [[(0, 0), (20, 0)], [(50, 50), (50, 70)]], sleep=lambda s: None,
            on_point=lambda x, y, down: seen.append(down))
kinds = [e[0] for e in pen.events]
check("each stroke is hover, down, moves, up", (kinds[0], kinds[1], kinds[kinds.index("up") - 1], kinds.count("up")),
      ("hover", "down", "move", 2))
check("the pen lifts between strokes", kinds[kinds.index("up") + 1], "hover")
check_true("the on-screen pen follows every point", len(seen) >= len(pen.events) - 2)

# the mouse pen: button down, moves, button up, through ydotool
import screen_control as sc  # noqa: E402
calls = []
sc.move_pointer = lambda px, py, timeout=1.5: calls.append(("move", px, py)) or "ok"
guard = Guard()
mouse = stylus.MousePen(guard, runner=lambda args, t: calls.append(tuple(args)) or "ok")
stylus.draw(mouse, [[(10, 10), (30, 10)]], sleep=lambda s: None)
press = calls.index(("click", "0x40")) if ("click", "0x40") in calls else -1
check_true("the mouse pen moves to the start, then presses the left button down",
           press > 0 and calls[press - 1] == ("move", 10, 10) and calls[press + 1][0] == "move")
check("and lets it go at the end", calls[-1], ("click", "0x80"))
guard.enabled = False
try:
    stylus.draw(mouse, [[(0, 0), (5, 5)]], sleep=lambda s: None)
    failures.append("the pen moved without mouse-and-keyboard permission")
except PermissionError:
    pass
guard.enabled = True

# the tablet pen, against a stand-in for python-evdev
written_events = []


class FakeUInput:
    def __init__(self, caps, name, input_props):
        self.caps, self.name, self.props = caps, name, input_props

    def write(self, etype, code, value):
        written_events.append((etype, code, value))

    def syn(self):
        written_events.append(("syn",))

    def close(self):
        written_events.append(("closed",))


codes = types.SimpleNamespace(EV_KEY=1, EV_ABS=3, BTN_TOOL_PEN=320, BTN_TOUCH=330, BTN_STYLUS=331,
                              ABS_X=0, ABS_Y=1, ABS_PRESSURE=24, INPUT_PROP_DIRECT=1)
fake_evdev = types.SimpleNamespace(ecodes=codes, UInput=FakeUInput, AbsInfo=lambda *a: a)
stylus.time = types.SimpleNamespace(sleep=lambda s: None)
tablet = stylus.TabletPen(guard, 1000, 800, evdev_module=fake_evdev)
stylus.draw(tablet, [[(500, 400), (520, 400)]], sleep=lambda s: None)
keys = [(c, v) for e in written_events if e[0] == 1 for (_t, c, v) in [e]]
check("the tablet brings the pen near, touches, and lifts", keys, [(320, 1), (330, 1), (330, 0)])
pressures = [v for e in written_events if e[0] == 3 and e[1] == 24 for v in [e[2]]]
check_true("with real pressure while touching", max(pressures) > 3000 and pressures[-1] == 0)
xs = [v for e in written_events if e[0] == 3 and e[1] == 0 for v in [e[2]]]
check_true("mapped onto the screen", abs(xs[0] - 16383) < 40)
tablet.close()
check("closing takes the pen away and removes the device", written_events[-1], ("closed",))

# -- seeing: real OCR on a rendered page -------------------------------------------------------------
workdir = Path(tempfile.mkdtemp())
W, H = 1000, 800
page = cairo.ImageSurface(cairo.FORMAT_RGB24, W, H)


def render_page():
    cr = cairo.Context(page)
    cr.set_source_rgb(1, 1, 1)
    cr.paint()
    cr.set_source_rgb(0, 0, 0)
    cr.select_font_face("DejaVu Sans")
    cr.set_font_size(28)
    cr.move_to(80, 120)
    cr.show_text("Worksheet 4")
    cr.set_font_size(22)
    cr.move_to(80, 220)
    cr.show_text("Total due: 42.50")
    cr.move_to(80, 300)
    cr.show_text("The mitochondria is the powerhouse of the cell.")
    # (coloured buttons are read through the accessibility tree instead:
    # see tests/test_accessibility.py; OCR reads text on the page)
    cr.set_source_rgb(0.1, 0.25, 0.7)
    for label, bx in (("Submit", 620), ("Cancel", 820)):
        cr.move_to(bx, 698)
        cr.show_text(label)


render_page()
shots = []


def fake_capture(logical_size, runner=None):
    path = workdir / f"shot{len(shots)}.png"
    page.write_to_png(str(path))
    shots.append(path)
    return path, logical_size[0] / W


vision.capture = fake_capture
have_tesseract = bool(shutil.which("tesseract"))
if have_tesseract:
    snap = vision.look((W, H))
    texts = [e["text"] for e in snap.elements]
    check_true(f"the screen's text is read ({texts})", any("42.50" in t for t in texts))
    check_true("things side by side on one row are separate", "Submit" in texts and "Cancel" in texts)
    total = vision.find(snap, "42.50")
    check_true(f"a number is found where it is drawn ({total})",
               total and 180 < total[0] < 230 and total[2] < 90 and 195 < total[1] < 225)
    submit = vision.find(snap, "submit")
    check_true(f"a link is found by its word ({submit})", submit and 600 < submit[0] < 700 and 670 < submit[1] < 700)
    by_id = [e for e in snap.elements if "Cancel" in e["text"]][0]["id"]
    check("an element can be named by its number", vision.find(snap, f"#{by_id}"),
          [e for e in snap.elements if e["id"] == by_id][0]["box"])
    check("something not on screen isn't guessed", vision.find(snap, "Download report"), None)
    observation = vision.describe(snap)
    check_true("the model gets numbered elements with positions", "#1:" in observation and " @ 0." in observation)
else:
    print("(tesseract not installed: skipped reading a real screen)")

# a canned tesseract result, to test the parsing on any machine
tsv = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
       "5\t1\t1\t1\t1\t1\t10\t10\t40\t12\t95\tTotal\n"
       "5\t1\t1\t1\t1\t2\t54\t10\t30\t12\t94\tdue:\n"
       "5\t1\t1\t1\t1\t3\t90\t10\t40\t12\t96\t42.50\n"
       "5\t1\t1\t1\t1\t4\t400\t10\t50\t12\t20\tnoise\n"
       "5\t1\t2\t1\t1\t1\t300\t60\t50\t14\t92\tSubmit\n")
words = vision.parse_tsv(tsv)
check("low-confidence guesses are dropped", [w["text"] for w in words], ["Total", "due:", "42.50", "Submit"])
els = vision.group_lines(words)
check("words on a line join up", [e["text"] for e in els], ["Total due: 42.50", "Submit"])
check("parsed vision-model boxes, as fractions", vision.parse_box("[0.1, 0.2, 0.3, 0.4]", (1000, 800)),
      (0.1, 0.2, 0.19999999999999998, 0.2))
check("or as pixels of the image", tuple(round(v, 6) for v in vision.parse_box("box: [100, 80, 300, 160]", (1000, 800))),
      (0.1, 0.1, 0.2, 0.1))
check("or nothing", vision.parse_box("none", (1000, 800)), None)

# -- the whole loop: look, find, circle with the pen, check it took -------------------------------------
class InkingPen(stylus.Pen):
    """A drawing app: ink appears wherever the pen touches and moves."""
    name = "pen"

    def __init__(self, paints=True):
        self.paints = paints
        self.last = None

    def hover(self, x, y):
        self.last = None

    def down(self, x, y, pressure=0.8):
        self.last = (x, y)

    def move(self, x, y, pressure=0.8):
        if self.paints and self.last is not None:
            cr = cairo.Context(page)
            cr.set_source_rgb(0.9, 0.1, 0.1)
            cr.set_line_width(3)
            cr.move_to(*self.last)
            cr.line_to(x, y)
            cr.stroke()
        self.last = (x, y)

    def up(self, x, y):
        self.last = None


class Control(Guard):
    def __init__(self):
        super().__init__()
        self.moves, self.clicks = [], []

    def pointer_pixels(self):
        return (500.0, 400.0)

    def get_screen_size(self):
        return (W, H)

    def move(self, fx, fy):
        self.moves.append((round(fx, 3), round(fy, 3)))
        return "Moved"

    def click(self, button="left"):
        self.clicks.append(button)
        return f"Clicked {button}"


cursor_states = []
work_mode.time = types.SimpleNamespace(sleep=lambda s: None, time=__import__("time").time)
stylus.time = types.SimpleNamespace(sleep=lambda s: None)
control = Control()
wm = work_mode.WorkMode(control, cursor=lambda x, y, state, box=None: cursor_states.append(state))
if have_tesseract:
    wm._pen = InkingPen(paints=True)
    result = wm.draw("circle", "42.50")
    check_true(f"circling reports the page changed ({result})", "the page changed there" in result)
    check_true("the pen cursor showed Toby looking, then drawing", "thinking" in cursor_states and "drawing" in cursor_states)
    page.write_to_png(str(workdir / "after-circle.png"))

    render_page()
    wm._pen = InkingPen(paints=False)
    result = wm.draw("underline", "powerhouse")
    check_true(f"an app that ignores the pen is reported honestly ({result})", "nothing changed on screen" in result)

    render_page()
    wm._pen = InkingPen(paints=True)
    result = wm.write_by_hand("Good", "42.50", where="right")
    check_true(f"writing by hand beside a target works ({result})", "page changed" in result)

    render_page()
    result = wm.click_on("Submit")
    check("clicking a button by its word moves there and clicks", (len(control.moves), control.clicks), (1, ["left"]))
    fx, fy = control.moves[0]
    check_true(f"at the button ({fx}, {fy})", 0.6 < fx < 0.8 and 0.82 < fy < 0.9)
    try:
        wm.click_on("Download report")
        failures.append("clicking something that isn't there should say so")
    except LookupError as e:
        check_true("with a sentence the model can use", "Couldn't find" in str(e))
    wm.click_on(x=0.25, y=0.5)
    check("or at a position", control.moves[-1], (0.25, 0.5))

if failures:
    print(f"{len(failures)} PROBLEM(S):")
    for f in failures:
        print("  ", f)
    sys.exit(1)
print(f"work mode checks passed (the circled page is {workdir / 'after-circle.png'})")
