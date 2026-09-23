"""
strokes.py — the paths a pen follows in Work Mode.

Pure geometry, no input devices: every function returns strokes, each a list
of (x, y) points in screen pixels, which stylus.py then plays through a real
pen or mouse. Keeping it separate means the shapes can be tested, drawn to
an image and looked at, without touching a screen.

Handwriting uses Hershey fonts (see hershey/README.md), which are made of
single pen strokes rather than outlines, so the pen writes letters the way a
hand would instead of tracing around them. The shapes are drawn loosely, the
way a person marks up a page: a circle overlaps where it started, an
underline sags a little.
"""

import json
import math
import random
from pathlib import Path

FONT_DIR = Path(__file__).resolve().parent / "hershey"
_FONTS = {}


def load_font(name="scripts"):
    """Glyphs keyed by character: (left, right, [strokes of (x, y)]), plus the
    font's cap and base lines. Coordinates are Hershey units, y downwards."""
    if name in _FONTS:
        return _FONTS[name]
    lines = (FONT_DIR / f"{name}.jhf").read_text().splitlines()
    metrics = {"define_cap_line": -12.0, "define_base_line": 9.0}
    glyphs = {}
    code = 32
    for line in lines:
        if line.startswith("#"):
            try:
                metrics.update(json.loads(line[1:]))
            except ValueError:
                pass
            continue
        if len(line) < 10:
            continue
        body = line[8:]
        left, right = ord(body[0]) - ord("R"), ord(body[1]) - ord("R")
        strokes, current = [], []
        for i in range(2, len(body) - 1, 2):
            pair = body[i:i + 2]
            if pair == " R":
                if current:
                    strokes.append(current)
                current = []
                continue
            current.append((ord(pair[0]) - ord("R"), ord(pair[1]) - ord("R")))
        if current:
            strokes.append(current)
        glyphs[chr(code)] = (left, right, strokes)
        code += 1
    font = {"glyphs": glyphs, "cap": metrics["define_cap_line"], "base": metrics["define_base_line"]}
    _FONTS[name] = font
    return font


def text_width(text, height, font="scripts"):
    f = load_font(font)
    scale = height / (f["base"] - f["cap"])
    return sum((f["glyphs"].get(ch, f["glyphs"]["?"])[1] - f["glyphs"].get(ch, f["glyphs"]["?"])[0])
               for ch in text) * scale


def handwriting(text, x, y, height=22.0, max_width=None, font="scripts", line_gap=1.6):
    """Strokes that write text with its first baseline starting at (x, y).

    height is the capital height in pixels. Long text wraps at max_width,
    on word boundaries.
    """
    f = load_font(font)
    scale = height / (f["base"] - f["cap"])
    lines = [str(text)] if not max_width else _wrap(str(text), height, max_width, font)
    out = []
    for n, line in enumerate(lines):
        cursor = x
        base_y = y + n * height * line_gap
        for ch in line:
            left, right, glyph = f["glyphs"].get(ch, f["glyphs"]["?"])
            for stroke in glyph:
                out.append([(cursor + (gx - left) * scale, base_y + (gy - f["base"]) * scale)
                            for gx, gy in stroke])
            cursor += (right - left) * scale
    return out


def _wrap(text, height, max_width, font):
    lines, current = [], ""
    for word in text.split():
        trial = (current + " " + word).strip()
        if current and text_width(trial, height, font) > max_width:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines or [""]


# ---------------------------------------------------------------------------
# Marks
# ---------------------------------------------------------------------------

def circle_around(box, pad=8.0, seed=0):
    """A hand-drawn loop around box=(x, y, w, h): it starts at the upper left,
    goes all the way round and overlaps its start a little."""
    rng = random.Random(seed)
    x, y, w, h = box
    cx, cy = x + w / 2, y + h / 2
    rx, ry = w / 2 + pad, max(h / 2 + pad, 14.0)
    start = math.radians(200)
    sweep = math.radians(385)
    perimeter = 2 * math.pi * math.sqrt((rx * rx + ry * ry) / 2)
    n = max(36, int(perimeter / 5))
    wobble_phase = rng.uniform(0, 2 * math.pi)
    points = []
    for i in range(n + 1):
        t = i / n
        a = start + sweep * t
        k = 1 + 0.035 * math.sin(3 * a + wobble_phase) + 0.02 * t    # drifts outward as it closes
        points.append((cx + math.cos(a) * rx * k, cy + math.sin(a) * ry * k))
    return [points]


def underline(box, gap=4.0, sag=1.5):
    x, y, w, h = box
    base = y + h + gap
    n = max(8, int(w / 6))
    return [[(x - 2 + (w + 4) * i / n, base + sag * math.sin(math.pi * i / n)) for i in range(n + 1)]]


def strike(box):
    x, y, w, h = box
    mid = y + h * 0.55
    n = max(8, int(w / 6))
    return [[(x - 2 + (w + 4) * i / n, mid - 0.6 * math.sin(math.pi * i / n)) for i in range(n + 1)]]


def highlight(box):
    """One stroke along the middle of the text; the app's highlighter tool
    gives it its width and colour."""
    x, y, w, h = box
    mid = y + h / 2
    n = max(8, int(w / 6))
    return [[(x + w * i / n, mid) for i in range(n + 1)]]


def box_around(box, pad=6.0, radius=6.0):
    x, y, w, h = box
    x0, y0, x1, y1 = x - pad, y - pad, x + w + pad, y + h + pad
    r = min(radius, (x1 - x0) / 2, (y1 - y0) / 2)
    pts = []
    corners = [(x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0), (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)]
    pts.append((x0 + r, y0))
    for cx, cy, a0 in corners:
        for i in range(7):
            a = math.radians(a0 + 90 * i / 6)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    pts.append((x0 + r + 2, y0))
    return [pts]


def line(p0, p1, spacing=5.0):
    (x0, y0), (x1, y1) = p0, p1
    n = max(2, int(math.hypot(x1 - x0, y1 - y0) / spacing))
    return [(x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n) for i in range(n + 1)]


def arrow(start, end, head=14.0):
    """A line from start to end with a two-stroke arrowhead at end."""
    (x0, y0), (x1, y1) = start, end
    angle = math.atan2(y1 - y0, x1 - x0)
    wings = []
    for side in (-1, 1):
        a = angle + math.pi + side * math.radians(28)
        wings.append(line((x1, y1), (x1 + head * math.cos(a), y1 + head * math.sin(a))))
    return [line(start, end)] + wings


def check_mark(box):
    x, y, w, h = box
    size = max(14.0, min(w, h, 40.0))
    cx, cy = x + w / 2, y + h / 2
    a = (cx - size * 0.45, cy)
    b = (cx - size * 0.1, cy + size * 0.38)
    c = (cx + size * 0.5, cy - size * 0.45)
    return [line(a, b, 3) + line(b, c, 3)[1:]]


def cross_mark(box):
    x, y, w, h = box
    size = max(14.0, min(w, h, 36.0))
    cx, cy = x + w / 2, y + h / 2
    s = size / 2
    return [line((cx - s, cy - s), (cx + s, cy + s), 3), line((cx + s, cy - s), (cx - s, cy + s), 3)]


SHAPES = {"circle": circle_around, "underline": underline, "strike": strike,
          "highlight": highlight, "box": box_around, "check": check_mark, "cross": cross_mark}


def shape(name, box):
    fn = SHAPES.get(str(name).lower())
    if fn is None:
        raise ValueError(f"unknown shape {name!r}; one of {', '.join(sorted(SHAPES))}")
    return fn(box)


# ---------------------------------------------------------------------------
# Timing and pressure
# ---------------------------------------------------------------------------

def resample(stroke, spacing=4.0):
    """Evenly spaced points along a stroke, so the pen moves at a steady pace."""
    if len(stroke) < 2:
        return list(stroke)
    out = [stroke[0]]
    carry = 0.0
    for (x0, y0), (x1, y1) in zip(stroke, stroke[1:]):
        seg = math.hypot(x1 - x0, y1 - y0)
        d = spacing - carry
        while d <= seg:
            t = d / seg
            out.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
            d += spacing
        carry = seg - (d - spacing)
    if out[-1] != stroke[-1]:
        out.append(stroke[-1])
    return out


def pressure(t):
    """Pen pressure 0..1 along a stroke: pressing in, steady, lifting off."""
    ramp = 0.1
    if t < ramp:
        return 0.35 + 0.45 * (t / ramp)
    if t > 1 - ramp:
        return 0.35 + 0.45 * ((1 - t) / ramp)
    return 0.8


def bounds(strokes):
    xs = [p[0] for s in strokes for p in s]
    ys = [p[1] for s in strokes for p in s]
    if not xs:
        return (0, 0, 0, 0)
    return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
