"""
chibi.py — Toby grows a body and does the task in front of you.

When Toby acts on the desktop, the round face in the pill pops out, grows a
small body, and walks across the screen to wherever the next step happens.
The pointer is carried in its hand, so mouse movement has a visible cause
instead of the cursor drifting across the screen by itself. Typing gets a
typing pose, clicks get a tap, and a checklist beside it shows every step:
finished ones struck through, the current one highlighted, the rest plain.
When the job is done it cheers, the body folds away, and the head flies
back into the pill.

Three layers, so each can be tested on its own:

- ``draw_chibi``, ``draw_task_card``, ``draw_bubble``: pure Cairo drawing.
  The tests render them to images.
- ``ChibiDirector``: position, walking, poses and timing, with no GTK. The
  task runner talks to this; it answers "has it arrived yet".
- ``make_stage_class``: the full-screen, click-through overlay window that
  shows it all, and draws only while something is moving.
"""

import math
import threading
import time

import cairo

from toby_anim import (Spring, clamp01, ease_in_cubic, ease_in_out_cubic,
                       ease_out_back, ease_out_cubic, lerp)

# Toby's warm face, matching the Face widget in the main app exactly.
SKIN_LIGHT = (1.0, 0.85, 0.35)
SKIN_DARK = (1.0, 0.65, 0.15)
INK = (0.15, 0.10, 0.05)
BLUSH = (1.0, 0.45, 0.35)
# The card and bubble use the app's deep-space surface and ink tokens.
SURFACE = (0.059, 0.063, 0.133)      # #0f1022
SURFACE_EDGE = (1, 1, 1, 0.11)
INK_BRIGHT = (0.957, 0.961, 1.0)
INK_QUIET = (0.957, 0.961, 1.0, 0.48)
TONE_OK = (0.435, 0.902, 0.659)

POSES = ("idle", "walk", "reach", "press", "type", "talk", "cheer", "think")


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

def _rounded_rect(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def _limb(cr, x0, y0, x1, y1, width, rgb):
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_width(width)
    cr.set_source_rgb(*rgb)
    cr.move_to(x0, y0)
    cr.line_to(x1, y1)
    cr.stroke()


def chibi_extent(scale):
    """Half-width and total height the character can occupy, for dirty
    rectangles. Generous, since arms reach out."""
    r = 30 * scale
    return r * 3.2, r * 5.4


def draw_chibi(cr, x, y, scale=1.0, pose="idle", phase=0.0, body=1.0,
               facing=1, look=(0.0, 0.0), reach=(0.0, -1.0), mouth=0.3,
               blink=0.0, squash=0.0, accent=(0.353, 0.549, 1.0), alpha=1.0):
    """Draw Toby with his feet at (x, y).

    body   0..1 — how grown the body is; 0 is just the floating head.
    phase  a running clock driving the walk cycle, typing and talking.
    reach  unit vector the reaching arm points along (screen coordinates).
    squash tap reaction: positive squashes, negative stretches.
    """
    r = 30.0 * scale
    body = clamp01(body)
    grown = ease_out_back(body, 0.9) if body < 1 else 1.0

    leg_len = r * 0.95 * grown
    torso_h = r * 1.05 * grown
    torso_w = r * 1.25 * max(0.25, grown)

    bob = 0.0
    if pose == "walk":
        bob = abs(math.sin(phase * 2 * math.pi)) * r * 0.10
    elif pose in ("idle", "talk", "think"):
        bob = math.sin(phase * 2 * math.pi * 0.35) * r * 0.035
    elif pose == "cheer":
        bob = abs(math.sin(phase * 2 * math.pi * 0.9)) * r * 0.22

    sq = max(-0.25, min(0.25, squash))
    hip_y = y - leg_len - bob
    shoulder_y = hip_y - torso_h
    head_cy = shoulder_y - r * 0.78 * max(0.35, grown) - (r * 0.0 if body > 0 else 0)
    if body <= 0.001:
        head_cy = y - r  # a floating head sits on the ground point

    cr.save()
    cr.push_group()

    # contact shadow — shrinks as Toby hops up
    lift = clamp01(bob / (r * 0.3))
    cr.save()
    cr.translate(x, y + r * 0.08)
    cr.scale(1.0, 0.28)
    grad = cairo.RadialGradient(0, 0, 0, 0, 0, r * 1.1)
    grad.add_color_stop_rgba(0, 0, 0, 0, 0.32 * (1 - 0.5 * lift) * max(0.3, grown))
    grad.add_color_stop_rgba(1, 0, 0, 0, 0)
    cr.set_source(grad)
    cr.arc(0, 0, r * 1.1, 0, 2 * math.pi)
    cr.fill()
    cr.restore()

    hoodie = accent
    hoodie_dark = tuple(c * 0.72 for c in accent)
    limb_w = r * 0.26

    if body > 0.02:
        # -- legs ------------------------------------------------------------
        # Toby faces the viewer, so a walk is shown the way a front-on
        # cartoon walk is: feet taking turns to lift, stepping slightly
        # toward the direction of travel, never crossing.
        for side in (-1, 1):
            hx = x + side * torso_w * 0.24
            if pose == "walk":
                cycle = math.sin(phase * 2 * math.pi + (0 if side < 0 else math.pi))
                lift = max(0.0, cycle) * leg_len * 0.28
                fx = hx + facing * cycle * r * 0.12
            else:
                lift, fx = 0.0, hx
            fy = hip_y + leg_len - lift
            _limb(cr, hx, hip_y, fx, fy, limb_w, hoodie_dark)
            cr.set_source_rgb(0.12, 0.13, 0.22)  # little shoes
            cr.arc(fx + facing * r * 0.07, fy, r * 0.17, 0, 2 * math.pi)
            cr.fill()

        # -- torso (a small hoodie) -----------------------------------------
        cr.save()
        cr.translate(x, hip_y)
        cr.scale(1 + sq * 0.6, 1 - sq)
        _rounded_rect(cr, -torso_w / 2, -torso_h, torso_w, torso_h, r * 0.42)
        g = cairo.LinearGradient(0, -torso_h, 0, 0)
        g.add_color_stop_rgb(0, *hoodie)
        g.add_color_stop_rgb(1, *hoodie_dark)
        cr.set_source(g)
        cr.fill()
        # pocket
        cr.set_source_rgba(1, 1, 1, 0.16)
        _rounded_rect(cr, -torso_w * 0.28, -torso_h * 0.42, torso_w * 0.56, torso_h * 0.26, r * 0.1)
        cr.fill()
        cr.restore()

        # -- arms -------------------------------------------------------------
        arm_len = r * 0.9 * grown
        for side in (-1, 1):
            sx = x + side * torso_w * 0.46
            sy = shoulder_y + r * 0.2
            if pose == "walk":
                cycle = math.sin(phase * 2 * math.pi + (math.pi if side < 0 else 0))
                hx = sx + side * r * (0.22 + 0.12 * max(0.0, cycle))
                hy = sy + arm_len * (0.95 - 0.18 * max(0.0, cycle))
            elif pose == "cheer":
                wave = math.sin(phase * 2 * math.pi * 1.6) * 0.25
                hx, hy = sx + side * arm_len * (0.45 + wave), sy - arm_len * 0.95
            elif pose == "type":
                # both hands forward over an unseen keyboard, tapping in turn
                tap = abs(math.sin(phase * 2 * math.pi * 3.2 + (side + 1) * 0.8)) * r * 0.14
                hx, hy = sx - side * r * 0.12, sy + arm_len * 0.62 + tap
            elif pose in ("reach", "press") and side == facing:
                ux, uy = reach
                n = math.hypot(ux, uy) or 1.0
                ext = arm_len * (1.35 if pose == "reach" else 1.15)
                hx, hy = sx + ux / n * ext, sy + uy / n * ext
            elif pose == "think" and side == facing:
                hx, hy = sx - side * r * 0.35, sy - arm_len * 0.35  # hand to chin
            else:
                hx, hy = sx + side * r * 0.18, sy + arm_len
            _limb(cr, sx, sy, hx, hy, limb_w, hoodie)
            cr.set_source_rgb(*SKIN_DARK)
            cr.arc(hx, hy, r * 0.16, 0, 2 * math.pi)
            cr.fill()

    # -- head --------------------------------------------------------------
    cr.save()
    cr.translate(x, head_cy)
    cr.scale(1 + sq * 0.35, 1 - sq * 0.35)
    tilt = 0.06 * math.sin(phase * 2 * math.pi * 0.5) if pose in ("idle", "think", "talk") else 0.0
    if pose == "walk":
        tilt = 0.05 * facing
    cr.rotate(tilt)

    # a little sprout with a rainbow tip — the wake ring, worn as a hat
    cr.set_line_width(r * 0.07)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_source_rgb(*SKIN_DARK)
    cr.move_to(0, -r * 0.95)
    cr.curve_to(r * 0.05, -r * 1.2, r * 0.22, -r * 1.25, r * 0.25, -r * 1.32)
    cr.stroke()
    hue = (phase * 0.25) % 1.0
    cr.set_source_rgb(*_hsv(hue, 0.65, 1.0))
    cr.arc(r * 0.27, -r * 1.36, r * 0.1, 0, 2 * math.pi)
    cr.fill()

    grad = cairo.RadialGradient(-r * 0.3, -r * 0.3, r * 0.1, 0, 0, r)
    grad.add_color_stop_rgb(0, *SKIN_LIGHT)
    grad.add_color_stop_rgb(1, *SKIN_DARK)
    cr.set_source(grad)
    cr.arc(0, 0, r, 0, 2 * math.pi)
    cr.fill()

    lx, ly = look
    eye_r = r * 0.14
    eye_dx = r * 0.36
    eye_y = -r * 0.1 + ly * r * 0.12
    open_amt = max(0.08, 1.0 - clamp01(blink))
    if pose == "cheer":
        open_amt = 0.0  # happy closed eyes ^ ^
    for side in (-1, 1):
        ex = side * eye_dx + lx * r * 0.14
        if open_amt <= 0.0:
            cr.set_line_width(r * 0.07)
            cr.set_source_rgb(*INK)
            cr.arc(ex, eye_y + eye_r * 0.4, eye_r * 0.9, math.pi * 1.15, math.pi * 1.85)
            cr.stroke()
        else:
            cr.save()
            cr.translate(ex, eye_y)
            cr.scale(1.0, open_amt)
            cr.arc(0, 0, eye_r, 0, 2 * math.pi)
            cr.set_source_rgb(*INK)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 0.85)  # catch-light
            cr.arc(eye_r * 0.35, -eye_r * 0.35, eye_r * 0.32, 0, 2 * math.pi)
            cr.fill()
            cr.restore()
        # blush
        cr.set_source_rgba(*BLUSH, 0.35)
        cr.arc(side * r * 0.56, r * 0.2, r * 0.13, 0, 2 * math.pi)
        cr.fill()

    if pose == "talk":
        mouth = 0.35 + 0.45 * abs(math.sin(phase * 2 * math.pi * 2.2))
    elif pose == "cheer":
        mouth = 1.0
    elif pose == "think":
        mouth = -0.05
    mw = r * 0.5
    my = r * 0.36
    curve = mouth * r * 0.28
    cr.move_to(-mw / 2, my)
    cr.curve_to(-mw / 4, my + curve, mw / 4, my + curve, mw / 2, my)
    cr.set_line_width(r * 0.075)
    cr.set_source_rgb(*INK)
    cr.stroke()
    cr.restore()

    cr.pop_group_to_source()
    cr.paint_with_alpha(clamp01(alpha))
    cr.restore()

    return head_cy


def _hsv(h, s, v):
    i = int(h * 6)
    f = h * 6 - i
    p, q, t = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)
    return [(v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q)][i % 6]


CARD_WIDTH = 300


def task_card_size(title, steps):
    return CARD_WIDTH, 58 + 30 * max(1, len(steps))


def draw_task_card(cr, x, y, title, steps, phase=0.0, alpha=1.0, accent=(0.353, 0.549, 1.0)):
    """The checklist of what Toby is doing.

    steps is a list of (label, status), status one of done/current/pending/
    error. Finished steps are struck through and dimmed, the current one is
    bright with a pulsing marker, and ones still to come are plain.
    """
    w, h = task_card_size(title, steps)
    cr.save()
    cr.push_group()

    # soft shadow, then the card
    for spread, a in ((14, 0.10), (7, 0.16)):
        _rounded_rect(cr, x - spread / 2, y - spread / 2 + 6, w + spread, h + spread, 20 + spread / 2)
        cr.set_source_rgba(0, 0, 0, a)
        cr.fill()
    _rounded_rect(cr, x, y, w, h, 18)
    cr.set_source_rgba(*SURFACE, 0.96)
    cr.fill_preserve()
    cr.set_source_rgba(*SURFACE_EDGE)
    cr.set_line_width(1)
    cr.stroke()

    cr.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    cr.set_font_size(14)
    cr.set_source_rgb(*INK_BRIGHT)
    cr.move_to(x + 18, y + 30)
    cr.show_text(_fit(cr, title, w - 36))

    done = sum(1 for _l, s in steps if s == "done")
    total = max(1, len(steps))
    # progress hairline under the title
    cr.set_source_rgba(1, 1, 1, 0.08)
    _rounded_rect(cr, x + 18, y + 40, w - 36, 3, 1.5)
    cr.fill()
    cr.set_source_rgb(*accent)
    _rounded_rect(cr, x + 18, y + 40, (w - 36) * done / total, 3, 1.5)
    cr.fill()

    row_y = y + 70
    for label, status in steps:
        cx, cy = x + 28, row_y - 5
        cr.new_path()
        if status == "done":
            cr.set_source_rgb(*TONE_OK)
            cr.arc(cx, cy, 7, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgb(*SURFACE)
            cr.set_line_width(2)
            cr.move_to(cx - 3.5, cy)
            cr.line_to(cx - 1, cy + 2.8)
            cr.line_to(cx + 3.8, cy - 2.8)
            cr.stroke()
        elif status == "current":
            pulse = 0.55 + 0.45 * math.sin(phase * 2 * math.pi * 1.2)
            cr.set_source_rgba(*accent, 0.25 * pulse)
            cr.arc(cx, cy, 10, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgb(*accent)
            cr.arc(cx, cy, 5.5, 0, 2 * math.pi)
            cr.fill()
        elif status == "error":
            cr.set_source_rgb(1.0, 0.5, 0.5)
            cr.set_line_width(2)
            cr.move_to(cx - 4, cy - 4); cr.line_to(cx + 4, cy + 4)
            cr.move_to(cx + 4, cy - 4); cr.line_to(cx - 4, cy + 4)
            cr.stroke()
        else:
            cr.set_source_rgba(1, 1, 1, 0.28)
            cr.set_line_width(1.5)
            cr.new_sub_path()
            cr.arc(cx, cy, 6, 0, 2 * math.pi)
            cr.stroke()

        weight = cairo.FONT_WEIGHT_BOLD if status == "current" else cairo.FONT_WEIGHT_NORMAL
        cr.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, weight)
        cr.set_font_size(13)
        text = _fit(cr, label, w - 64)
        if status == "done":
            cr.set_source_rgba(*INK_QUIET)
        elif status == "error":
            cr.set_source_rgba(1.0, 0.6, 0.6, 0.9)
        elif status == "current":
            cr.set_source_rgb(*INK_BRIGHT)
        else:
            cr.set_source_rgba(0.957, 0.961, 1.0, 0.78)
        tx = x + 46
        cr.move_to(tx, row_y)
        cr.show_text(text)
        if status == "done":
            # the strike-through
            ext = cr.text_extents(text)
            cr.set_line_width(1.4)
            cr.set_source_rgba(*INK_QUIET)
            cr.move_to(tx, row_y - 4.5)
            cr.line_to(tx + ext.x_advance, row_y - 4.5)
            cr.stroke()
        row_y += 30

    cr.pop_group_to_source()
    cr.paint_with_alpha(clamp01(alpha))
    cr.restore()
    return w, h


def _fit(cr, text, max_w):
    if cr.text_extents(text).x_advance <= max_w:
        return text
    while text and cr.text_extents(text + "…").x_advance > max_w:
        text = text[:-1]
    return text + "…"


def bubble_size(cr, text, max_w=260):
    lines = _wrap(cr, text, max_w)
    return min(max_w, max(cr.text_extents(l).x_advance for l in lines) if lines else 40) + 28, 20 * len(lines) + 22


def _wrap(cr, text, max_w, max_lines=4):
    words = str(text).split()
    lines, line = [], ""
    for word in words:
        trial = (line + " " + word).strip()
        if cr.text_extents(trial).x_advance <= max_w or not line:
            line = trial
        else:
            lines.append(line)
            line = word
        if len(lines) >= max_lines:
            break
    if line and len(lines) < max_lines:
        lines.append(line)
    if len(lines) == max_lines and len(" ".join(lines)) < len(" ".join(words)):
        lines[-1] = _fit(cr, lines[-1] + "…", max_w)
    return lines


def draw_bubble(cr, tail_x, tail_y, text, alpha=1.0):
    """A speech bubble whose tail points at (tail_x, tail_y), above it."""
    if not text:
        return
    cr.save()
    cr.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    cr.set_font_size(13)
    w, h = bubble_size(cr, text)
    x = tail_x - w / 2
    y = tail_y - h - 12
    cr.push_group()
    _rounded_rect(cr, x, y, w, h, 14)
    cr.move_to(tail_x - 8, y + h - 1)
    cr.line_to(tail_x, tail_y)
    cr.line_to(tail_x + 8, y + h - 1)
    cr.close_path()
    cr.set_source_rgba(*SURFACE, 0.95)
    cr.fill()
    cr.set_source_rgb(*INK_BRIGHT)
    for i, line in enumerate(_wrap(cr, text, 260)):
        cr.move_to(x + 14, y + 25 + i * 20)
        cr.show_text(line)
    cr.pop_group_to_source()
    cr.paint_with_alpha(clamp01(alpha))
    cr.restore()


# ---------------------------------------------------------------------------
# Motion and timing — no GTK
# ---------------------------------------------------------------------------

class ChibiDirector:
    """Where Toby is, what he's doing, and when he gets there.

    The task runner works on a background thread and must never be able to
    hang waiting for an animation, so every wait here is bounded: walk_to()
    returns an Event that is set on arrival *or* when a timeout passes,
    whichever comes first, and stop() sets every pending event at once.
    """

    HIDDEN, EMERGING, ACTIVE, RETURNING = "hidden", "emerging", "active", "returning"

    def __init__(self, settings, clock=time.monotonic):
        self.clock = clock
        self.settings = settings
        self.state = self.HIDDEN
        self.x = self.y = 0.0
        self.home = (0.0, 0.0)
        self.target = None
        self.facing = 1
        self.pose = "idle"
        self.body = 0.0            # grown amount 0..1
        self.alpha = 0.0
        self.phase = 0.0
        self.squash = Spring(0.0, stiffness=380, damping=18)
        self.look = (0.0, 0.0)
        self.reach = (0.0, -1.0)
        self.blink = 0.0
        self._next_blink = 2.5
        self.bubble = ""
        self.title = ""
        self.steps = []
        self._arrival = None
        self._pose_until = None
        self._pose_after = "idle"
        self._t_state = clock()
        self._last = clock()
        self._lock = threading.Lock()

    # -- commands (called on the GTK thread) ------------------------------------
    def reload(self, settings):
        self.settings = settings

    def emerge(self, home_x, home_y, title, steps):
        """Pop out of the pill at (home_x, home_y) and grow a body."""
        self.home = (home_x, home_y)
        if self.state == self.HIDDEN:
            self.x, self.y = home_x, home_y
        self.state = self.EMERGING
        self._t_state = self.clock()
        self.title, self.steps = title, list(steps)
        self.pose = "idle"
        self.squash.kick(-3.0)  # a little stretch as it pops up

    def set_steps(self, title, steps):
        self.title, self.steps = title, list(steps)

    def walk_to(self, x, y, event=None):
        """Walk until the reaching hand is near (x, y). Returns an Event.

        A caller on another thread can create the Event itself and pass it
        in, so it can start waiting before this runs on the GTK thread.
        """
        event = event or threading.Event()
        with self._lock:
            if self._arrival is not None:
                self._arrival.set()
            self._arrival = event
        # stand a little to the side of the target so the hand, not the
        # body, lands on it — and so Toby never hides what he's clicking
        side = -1 if x > self.x else 1
        self.target = (x + side * 58, y + 118)
        self.facing = -side
        self._reach_target = (x, y)
        self.pose = "walk"
        distance = math.hypot(self.target[0] - self.x, self.target[1] - self.y)
        timeout = 0.4 + distance / max(100.0, self.settings.get("chibi_walk_speed", 900.0)) * 1.6
        self._arrival_deadline = self.clock() + min(3.0, timeout)
        return event

    def perform(self, pose, seconds=0.45, then="idle"):
        self.pose = pose
        self._pose_until = self.clock() + seconds
        self._pose_after = then
        if pose == "press":
            self.squash.kick(4.5)

    def say(self, text):
        self.bubble = text or ""

    def finish(self):
        """Cheer, then fold the body away and fly home."""
        self.bubble = ""
        self.perform("cheer", 0.7, then="idle")
        self._finish_at = self.clock() + 0.7

    def stop(self):
        """Drop everything immediately and release anyone waiting."""
        with self._lock:
            if self._arrival is not None:
                self._arrival.set()
                self._arrival = None
        self.state = self.HIDDEN
        self.body = 0.0
        self.alpha = 0.0
        self.target = None

    @property
    def visible(self):
        return self.state != self.HIDDEN

    # -- per frame ---------------------------------------------------------------
    def step(self):
        now = self.clock()
        dt = min(0.05, max(0.0, now - self._last))
        self._last = now
        self.phase += dt
        self.squash.step(dt)

        # blinking
        self._next_blink -= dt
        if self._next_blink <= 0:
            self.blink = 1.0
            self._next_blink = 2.2 + (hash(int(now * 10)) % 30) / 10.0
        self.blink = max(0.0, self.blink - dt * 9)

        transform = max(0.1, self.settings.get("chibi_transform_duration", 0.55))
        since = now - self._t_state

        if self.state == self.EMERGING:
            t = clamp01(since / transform)
            self.alpha = ease_out_cubic(clamp01(t * 2.5))
            self.body = t
            # rise up out of the pill as the body grows beneath the head
            self.y = self.home[1] - ease_out_cubic(t) * 40
            if t >= 1.0:
                self.state = self.ACTIVE
        elif self.state == self.RETURNING:
            t = clamp01(since / transform)
            self.body = 1.0 - ease_in_out_cubic(t)
            self.x = lerp(self._return_from[0], self.home[0], ease_in_out_cubic(t))
            self.y = lerp(self._return_from[1], self.home[1], ease_in_out_cubic(t))
            self.alpha = 1.0 - ease_in_cubic(clamp01((t - 0.55) / 0.45))
            if t >= 1.0:
                self.stop()
                return False

        # walking
        if self.target is not None and self.state in (self.ACTIVE, self.EMERGING):
            tx, ty = self.target
            dx, dy = tx - self.x, ty - self.y
            dist = math.hypot(dx, dy)
            speed = self.settings.get("chibi_walk_speed", 900.0)
            # ease in and out of a walk: slow near the destination
            v = speed * min(1.0, 0.25 + dist / 220.0)
            if dist <= max(2.0, v * dt):
                self.x, self.y = tx, ty
                self._arrive()
            else:
                self.x += dx / dist * v * dt
                self.y += dy / dist * v * dt
                if abs(dx) > 1:
                    self.facing = 1 if dx > 0 else -1
        if self._arrival is not None and now >= getattr(self, "_arrival_deadline", now + 1):
            self._arrive()  # never let a caller wait forever

        # timed poses
        if self._pose_until is not None and now >= self._pose_until:
            self._pose_until = None
            self.pose = self._pose_after
        if getattr(self, "_finish_at", None) is not None and now >= self._finish_at:
            self._finish_at = None
            self._return_from = (self.x, self.y)
            self.state = self.RETURNING
            self._t_state = now
            self.target = None

        # the reaching arm and the eyes follow the thing being worked on
        rt = getattr(self, "_reach_target", None)
        if rt is not None:
            hx, hy = self.x, self.y - 110
            vx, vy = rt[0] - hx, rt[1] - hy
            n = math.hypot(vx, vy) or 1.0
            self.reach = (vx / n, vy / n)
            self.look = (max(-1, min(1, vx / 300)), max(-1, min(1, vy / 300)))
        return self.state != self.HIDDEN

    def _arrive(self):
        self.target = None
        if self.pose == "walk":
            self.pose = "reach"
        with self._lock:
            if self._arrival is not None:
                self._arrival.set()
                self._arrival = None

    def hand_position(self):
        """Where the reaching hand is on screen — the pointer rides here."""
        rt = getattr(self, "_reach_target", None)
        return rt if rt is not None else (self.x, self.y - 110)

    def bounds(self, scale=1.0):
        """A generous box around everything drawn, for partial redraws."""
        half_w, height = chibi_extent(scale)
        x0, y0 = self.x - half_w - 20, self.y - height - 150
        x1, y1 = self.x + half_w + 20, self.y + 30
        rt = getattr(self, "_reach_target", None)
        if rt is not None:
            x0, y0 = min(x0, rt[0] - 30), min(y0, rt[1] - 30)
            x1, y1 = max(x1, rt[0] + 30), max(y1, rt[1] + 30)
        return int(x0), int(y0), int(x1 - x0), int(y1 - y0)


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------

def make_stage_class():
    """Build the stage window class; deferred so importing needs no GTK."""
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("GtkLayerShell", "0.1")
    gi.require_foreign("cairo")
    from gi.repository import GLib, Gtk, GtkLayerShell

    class ChibiStage(Gtk.Window):
        """A full-screen, click-through layer the chibi lives on.

        Hidden whenever Toby isn't acting. While shown, frames come from the
        compositor's frame clock, and only the area around the chibi, its
        card and its bubble is redrawn — a full-screen transparent surface
        repainted in full every frame would be a waste on integrated
        graphics.
        """

        def __init__(self, director, accent_getter=lambda: (0.353, 0.549, 1.0)):
            super().__init__()
            self.director = director
            self._accent = accent_getter
            GtkLayerShell.init_for_window(self)
            GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
            GtkLayerShell.set_namespace(self, "toby-chibi")
            for edge in (GtkLayerShell.Edge.TOP, GtkLayerShell.Edge.BOTTOM,
                         GtkLayerShell.Edge.LEFT, GtkLayerShell.Edge.RIGHT):
                GtkLayerShell.set_anchor(self, edge, True)
            GtkLayerShell.set_exclusive_zone(self, -1)
            GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)
            self.set_app_paintable(True)
            visual = self.get_screen().get_rgba_visual()
            if visual:
                self.set_visual(visual)
            self.set_decorated(False)
            self.area = Gtk.DrawingArea()
            self.area.connect("draw", self.on_draw)
            self.add(self.area)
            self._tick_id = None
            self._last_dirty = None

        def start(self):
            if not self.get_visible():
                self.show_all()
                window = self.get_window()
                if window is not None:
                    window.input_shape_combine_region(cairo.Region(), 0, 0)
            if self._tick_id is None:
                self._tick_id = self.area.add_tick_callback(self._on_tick)

        def _on_tick(self, _widget, _clock):
            alive = self.director.step()
            self._invalidate()
            if not alive:
                self._tick_id = None
                self.hide()
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE

        def _card_rect(self):
            w, h = task_card_size(self.director.title, self.director.steps)
            width = self.get_allocated_width()
            if width < 600:
                # Not configured to full size yet (the first frame after
                # mapping can arrive before the compositor sizes a layer
                # surface). Use the screen, not a default window width, or
                # the card lands off the left edge.
                width = self.get_screen().get_width()
            return max(12, width - w - 28), 72, w + 10, h + 20

        def _invalidate(self):
            d = self.director
            x, y, w, h = d.bounds()
            rects = [(x, y, w, h), self._card_rect()]
            if self._last_dirty:
                rects.append(self._last_dirty)
            for rx, ry, rw, rh in rects:
                self.area.queue_draw_area(rx, ry, rw, rh)
            self._last_dirty = (x, y, w, h)

        def on_draw(self, widget, cr):
            cr.set_operator(cairo.OPERATOR_SOURCE)
            cr.set_source_rgba(0, 0, 0, 0)
            cr.paint()
            cr.set_operator(cairo.OPERATOR_OVER)
            d = self.director
            if not d.visible:
                return False
            accent = self._accent()
            try:
                if d.steps:
                    cx, cy, _w, _h = self._card_rect()
                    draw_task_card(cr, cx, cy, d.title or "Working on it", d.steps,
                                   d.phase, d.alpha, accent)
                head_y = draw_chibi(cr, d.x, d.y, 1.0, d.pose, d.phase, d.body, d.facing,
                                    d.look, d.reach, blink=d.blink, squash=d.squash.value,
                                    accent=accent, alpha=d.alpha)
                if d.bubble:
                    draw_bubble(cr, d.x, head_y - 44, d.bubble, d.alpha)
            except Exception as e:
                print("CHIBI DRAW ERROR:", e, flush=True)
                d.stop()
            return False

    return ChibiStage
