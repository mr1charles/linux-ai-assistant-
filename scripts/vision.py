"""
vision.py — what Toby can see of the screen in Work Mode.

    screen -> screenshot (grim) -> text with positions (tesseract) and the
    open windows (Hyprland) -> numbered elements Toby can point at -> act
    -> screenshot again -> did the right part of the screen change?

Most of what a person points at on a screen has words on it: buttons,
menu items, fields' labels, the sentence or number in a document. Tesseract
reports every word it reads with its box, so "circle the total", "click
Submit" or "underline the second paragraph's first sentence" can be found
by their text, and the model gets a compact list of numbered elements
instead of an image it couldn't see anyway.

If a local vision model is installed in Ollama (for example qwen2.5vl or
llava) and chosen in settings as "vision_model", locate() can also ask it
for things without words, like an icon. That's optional and off by default.

All coordinates here are in the same logical screen pixels the pointer
uses; the screenshot's own pixel size is converted.
"""

import base64
import difflib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import requests


class Snapshot:
    """One look at the screen."""

    def __init__(self, png, scale, size, words, elements, windows, taken_at):
        self.png = png            # path to the screenshot
        self.scale = scale        # logical pixels per screenshot pixel
        self.size = size          # (width, height) in logical pixels
        self.words = words
        self.elements = elements
        self.windows = windows
        self.taken_at = taken_at

    def element(self, element_id):
        for el in self.elements:
            if el["id"] == element_id:
                return el
        return None


def _run(cmd, timeout=15, text=True):
    # Tesseract uses every core by default; two passes at once then spend
    # their time fighting over them (seconds instead of a fraction of one).
    env = dict(os.environ, OMP_THREAD_LIMIT="1") if cmd and cmd[0] == "tesseract" else None
    try:
        return subprocess.run(cmd, capture_output=True, text=text, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 127, "" if text else b"", str(e))


def capture(logical_size, runner=_run):
    """A full-resolution PNG of the screen and its scale to logical pixels."""
    if not shutil.which("grim"):
        raise RuntimeError("grim isn't installed, so Toby can't see the screen")
    path = Path(tempfile.mkdtemp(prefix="toby-look-")) / "screen.png"
    out = runner(["grim", "-t", "png", str(path)], timeout=10)
    if out.returncode != 0 or not path.exists():
        raise RuntimeError("couldn't take a screenshot")
    width, _height = png_size(path)
    scale = logical_size[0] / width if width else 1.0
    return path, scale


def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        return 0, 0
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def ocr_words(png, runner=_run):
    """Every word tesseract reads: text, confidence, box (screenshot pixels),
    and which line it belongs to.

    Tesseract reads dark text on a light background far better than the
    reverse, and a dark desktop is full of the reverse (buttons, menus, dark
    themes). So it reads the screenshot and an inverted copy at the same
    time, and keeps the words the second pass found that the first didn't.
    """
    if not shutil.which("tesseract"):
        raise RuntimeError("tesseract isn't installed, so Toby can't read the screen")
    inverted = _inverted_copy(png)
    results = {}

    def read(key, path):
        out = runner(["tesseract", str(path), "-", "--psm", "11", "tsv"], timeout=40)
        results[key] = parse_tsv(out.stdout, pass_id=key) if out.returncode == 0 else None

    import threading
    threads = [threading.Thread(target=read, args=("plain", png))]
    if inverted is not None:
        threads.append(threading.Thread(target=read, args=("inverted", inverted)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if results.get("plain") is None and not results.get("inverted"):
        raise RuntimeError("tesseract couldn't read the screenshot")
    words = list(results.get("plain") or [])
    for w in results.get("inverted") or []:
        if not any(_overlap(w["box"], k["box"]) > 0.3 for k in words):
            words.append(w)
    return words


def _inverted_copy(png):
    try:
        import cairo
        src = cairo.ImageSurface.create_from_png(str(png))
        out = cairo.ImageSurface(cairo.FORMAT_RGB24, src.get_width(), src.get_height())
        cr = cairo.Context(out)
        cr.set_source_rgb(1, 1, 1)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_DIFFERENCE)
        cr.set_source_surface(src, 0, 0)
        cr.paint()
        path = Path(str(png)).with_name("screen-inverted.png")
        out.write_to_png(str(path))
        return path
    except Exception:
        return None


def _overlap(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    smaller = min(aw * ah, bw * bh) or 1
    return inter / smaller


def parse_tsv(tsv, pass_id="plain"):
    words = []
    for line in tsv.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 12 or parts[0] != "5":
            continue
        text = parts[11].strip()
        try:
            conf = float(parts[10])
            left, top, width, height = (int(p) for p in parts[6:10])
        except ValueError:
            continue
        if not text or conf < 35:
            continue
        words.append({"text": text, "conf": conf, "box": (left, top, width, height),
                      "line": (pass_id, int(parts[2]), int(parts[3]), int(parts[4]))})
    return words


def group_lines(words):
    """Words on the same line, joined: the elements Toby can point at."""
    lines = {}
    for w in words:
        lines.setdefault(w["line"], []).append(w)
    elements = []
    for key in sorted(lines, key=lambda k: (min(w["box"][1] for w in lines[k]), min(w["box"][0] for w in lines[k]))):
        ws = sorted(lines[key], key=lambda w: w["box"][0])
        # split a "line" where there's a big horizontal gap: separate buttons
        # on one row are separate things to point at
        chunk = [ws[0]]
        for w in ws[1:]:
            prev = chunk[-1]["box"]
            gap = w["box"][0] - (prev[0] + prev[2])
            if gap > max(prev[3], w["box"][3]) * 2.2:
                elements.append(_merge(chunk))
                chunk = [w]
            else:
                chunk.append(w)
        elements.append(_merge(chunk))
    return elements


def _merge(words):
    x0 = min(w["box"][0] for w in words)
    y0 = min(w["box"][1] for w in words)
    x1 = max(w["box"][0] + w["box"][2] for w in words)
    y1 = max(w["box"][1] + w["box"][3] for w in words)
    return {"text": " ".join(w["text"] for w in words), "box": (x0, y0, x1 - x0, y1 - y0), "words": words}


def scale_box(box, scale):
    x, y, w, h = box
    return (x * scale, y * scale, w * scale, h * scale)


# ---------------------------------------------------------------------------
# The accessibility tree: buttons, menus and fields, by name
# ---------------------------------------------------------------------------

# Roles worth pointing at. Text OCR finds what's written in documents;
# this finds the controls, including the ones OCR can't read (light text on
# a coloured button, an icon with only a name).
CONTROL_ROLES = {"push button", "toggle button", "check box", "radio button", "menu item",
                 "check menu item", "radio menu item", "menu", "link", "entry", "text", "password text",
                 "combo box", "page tab", "list item", "slider", "spin button", "tool bar item",
                 "icon", "tree item", "table cell", "switch"}
MAX_NODES = 4000


def accessibility_elements(window=None, budget_s=2.5, atspi=None):
    """Controls in the focused app, from AT-SPI: name, role and box in
    logical screen pixels. [] if accessibility isn't available.

    window is the focused window as Hyprland reports it (pid, x, y). On
    Wayland apps can't know where their window is on screen, so boxes are
    taken relative to the window and moved by Hyprland's position for it.
    """
    if atspi is None:
        try:
            import gi
            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi as atspi
        except (ImportError, ValueError):
            return []
    try:
        atspi.set_timeout(600, 1500)       # never let a stuck app stall Toby
        desktop = atspi.get_desktop(0)
    except Exception:
        return []
    started = time.monotonic()
    pid = (window or {}).get("pid")
    offset = None
    if window and window.get("x") is not None:
        offset = (window["x"], window["y"])
    found = []
    for i in range(desktop.get_child_count()):
        try:
            app = desktop.get_child_at_index(i)
            if app is None or (pid and app.get_process_id() != pid):
                continue
        except Exception:
            continue
        stack, seen = [app], 0
        while stack and seen < MAX_NODES and time.monotonic() - started < budget_s:
            node = stack.pop()
            seen += 1
            try:
                states = node.get_state_set()
                if node is not app and not states.contains(atspi.StateType.SHOWING):
                    continue
                role = node.get_role_name()
                name = (node.get_name() or "").strip()
                if role in CONTROL_ROLES and name:
                    kind = atspi.CoordType.WINDOW if offset else atspi.CoordType.SCREEN
                    ext = node.get_extents(kind)
                    if ext.width > 0 and ext.height > 0:
                        x, y = ext.x, ext.y
                        if offset:
                            x, y = x + offset[0], y + offset[1]
                        found.append({"text": name, "role": role, "box": (x, y, ext.width, ext.height),
                                      "words": [], "source": "accessibility"})
                count = node.get_child_count()
                for c in range(min(count, 500) - 1, -1, -1):
                    child = node.get_child_at_index(c)
                    if child is not None:
                        stack.append(child)
            except Exception:
                continue
    return found


def look(logical_size, windows=None, runner=_run, accessibility=True):
    """Take a Snapshot: screenshot, words, numbered elements, windows.

    Elements come from the focused app's accessibility tree (its controls,
    by name) and from reading the screen's text; text that sits on a control
    already found is not listed twice.
    """
    png, scale = capture(logical_size, runner)
    words = ocr_words(png, runner)
    for w in words:
        w["box"] = scale_box(w["box"], scale)
    elements = []
    focused = next((w for w in (windows or []) if w.get("focused")), None)
    if accessibility and focused:
        elements = accessibility_elements(focused)
    text_elements = [el for el in group_lines(words)
                     if not any(_overlap(el["box"], c["box"]) > 0.5 for c in elements)]
    elements = sorted(elements + text_elements, key=lambda el: (round(el["box"][1] / 8), el["box"][0]))
    for i, el in enumerate(elements, start=1):
        el["id"] = i
    return Snapshot(png, scale, logical_size, words, elements, windows or [], time.time())


def describe(snapshot, limit=90):
    """The observation the model reads: windows, then numbered text on screen
    with positions as fractions of the screen (what move_mouse uses)."""
    w, h = snapshot.size
    lines = [f"Screen {int(w)}x{int(h)}."]
    focused = next((win for win in snapshot.windows if win.get("focused")), None)
    if focused:
        lines.append(f"Focused window: {focused['app']}, \"{focused['title'][:80]}\".")
    if snapshot.windows:
        others = [f"{win['app']}" for win in snapshot.windows if not win.get("focused")][:8]
        if others:
            lines.append("Other windows: " + ", ".join(others) + ".")
    if not snapshot.elements:
        lines.append("No readable text on screen.")
        return "\n".join(lines)
    lines.append("On screen (#id: [control type] text @ x,y as fractions of the screen):")
    for el in snapshot.elements[:limit]:
        x, y, bw, bh = el["box"]
        role = f"[{el['role']}] " if el.get("role") else ""
        lines.append(f"#{el['id']}: {role}{el['text'][:70]} @ {(x + bw / 2) / w:.3f},{(y + bh / 2) / h:.3f}")
    if len(snapshot.elements) > limit:
        lines.append(f"(and {len(snapshot.elements) - limit} more)")
    return "\n".join(lines)


def _norm(text):
    return re.sub(r"[^\w.%$€£:/-]+", " ", str(text).lower()).strip()


def find(snapshot, query):
    """The box of the thing a person means by query: an element number
    ("#12" or "12"), exact words or a number on screen ("42.50", "Submit"),
    or the closest wording. None if nothing on screen is a good match."""
    q = str(query or "").strip()
    if not q:
        return None
    m = re.fullmatch(r"#?(\d{1,4})", q)
    if m and snapshot.element(int(m.group(1))):
        return snapshot.element(int(m.group(1)))["box"]
    nq = _norm(q)
    if not nq:
        return None
    # a run of consecutive words that matches exactly (within one line)
    target = nq.split()
    for el in snapshot.elements:
        ws = el["words"]
        norms = [_norm(w["text"]) for w in ws]
        for i in range(len(ws) - len(target) + 1):
            if norms[i:i + len(target)] == target:
                return _merge(ws[i:i + len(target)])["box"]
    # a whole element containing it
    for el in snapshot.elements:
        if nq in _norm(el["text"]):
            return el["box"]
    # the closest wording, if it's close enough
    best, score = None, 0.0
    for el in snapshot.elements:
        s = difflib.SequenceMatcher(None, nq, _norm(el["text"])).ratio()
        if s > score:
            best, score = el, s
    return best["box"] if best and score >= 0.72 else None


def region_change(before_png, after_png, box, scale, step=2):
    """How much of box (logical pixels) differs between two screenshots,
    0..1. Used to check a mark actually appeared where it was drawn."""
    import cairo
    a = cairo.ImageSurface.create_from_png(str(before_png))
    b = cairo.ImageSurface.create_from_png(str(after_png))
    if (a.get_width(), a.get_height()) != (b.get_width(), b.get_height()):
        return 1.0
    x, y, w, h = (v / scale for v in box)
    x0, y0 = max(0, int(x)), max(0, int(y))
    x1, y1 = min(a.get_width(), int(x + w) + 1), min(a.get_height(), int(y + h) + 1)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    da, db = a.get_data(), b.get_data()
    stride = a.get_stride()
    changed = total = 0
    for py in range(y0, y1, step):
        row = py * stride
        for px in range(x0, x1, step):
            i = row + px * 4
            total += 1
            if abs(da[i] - db[i]) + abs(da[i + 1] - db[i + 1]) + abs(da[i + 2] - db[i + 2]) > 60:
                changed += 1
    return changed / total if total else 0.0


# ---------------------------------------------------------------------------
# Optional: a local vision model, for things without words
# ---------------------------------------------------------------------------

def parse_box(text, image_size):
    """A box from a vision model's answer: [x1, y1, x2, y2], as fractions or
    as pixels of the image it was shown. Returns (x, y, w, h) as fractions."""
    m = re.search(r"\[\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\]", str(text))
    if not m:
        return None
    x1, y1, x2, y2 = (float(v) for v in m.groups())
    if max(x1, y1, x2, y2) > 1.0:
        iw, ih = image_size
        x1, x2, y1, y2 = x1 / iw, x2 / iw, y1 / ih, y2 / ih
    x1, x2 = sorted((max(0.0, min(1.0, x1)), max(0.0, min(1.0, x2))))
    y1, y2 = sorted((max(0.0, min(1.0, y1)), max(0.0, min(1.0, y2))))
    if x2 - x1 <= 0 or y2 - y1 <= 0:
        return None
    return (x1, y1, x2 - x1, y2 - y1)


def locate(snapshot, description, model, ollama_url="http://localhost:11434/api/chat", post=requests.post):
    """Ask a local vision model where something is. Returns a box in logical
    pixels, or None if there's no model, or it couldn't say."""
    if not model:
        return None
    image = Path(snapshot.png).read_bytes()
    prompt = (f"Find this on the screenshot: {description}. Answer with only its bounding box as "
              "[x1, y1, x2, y2], each a fraction of the image width or height between 0 and 1. "
              "If it isn't there, answer none.")
    try:
        resp = post(ollama_url, json={"model": model, "stream": False, "options": {"temperature": 0},
                                      "messages": [{"role": "user", "content": prompt,
                                                    "images": [base64.b64encode(image).decode()]}]},
                    timeout=120)
        answer = resp.json().get("message", {}).get("content", "")
    except (requests.RequestException, ValueError):
        return None
    frac = parse_box(answer, png_size(snapshot.png))
    if frac is None:
        return None
    w, h = snapshot.size
    return (frac[0] * w, frac[1] * h, frac[2] * w, frac[3] * h)
