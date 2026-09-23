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

from toby_anim import (DURATIONS, EASE, Spring, clamp01, ease_in_cubic,
                       ease_in_out_cubic, ease_out_back, ease_out_cubic, lerp)

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

POSES = ("idle", "walk", "reach", "press", "type", "talk", "cheer", "think", "hold", "carry")


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


# Where Toby's hand holds the pointer, relative to its tip: the tail of the
# arrow, so the tip and whatever it points at stay uncovered.
GRIP = (7.0, 17.0)
# Where Toby stands relative to the hand holding something up: to one side
# and below, arm raised, so the hand reaches without the body covering it.
STAND = (41.0, 78.0)

# A little keyboard Toby types on. Digits share the top row's columns.
KEY_ROWS = ("qwertyuiop", "asdfghjkl;", "zxcvbnm,./")


def key_position(char):
    """(row, column) of a character on the little keyboard, or None."""
    if not char:
        return None
    char = char.lower()
    if char == " ":
        return (3, 4)
    if char.isdigit():
        return (0, (int(char) - 1) % 10)
    for row, keys in enumerate(KEY_ROWS):
        col = keys.find(char)
        if col >= 0:
            return (row, col)
    return None


def keyboard_geometry(cx, top, width):
    """Key size and a key-centre function for a keyboard at (cx, top)."""
    pitch = width / 10.0
    pad = pitch * 0.45
    left = cx - width / 2

    def centre(row, col):
        if row == 3:   # the space bar spans the middle
            return cx, top + pad + pitch * 3.5
        return left + pitch * (col + 0.5 + row * 0.25) - pitch * 0.12, top + pad + pitch * (row + 0.5)

    return pitch, pitch * 4 + pad * 2, centre


def draw_keyboard(cr, cx, top, width, amount=1.0, active=None, accent=(0.353, 0.549, 1.0)):
    """The little keyboard. active is a (row, col) being pressed, or None."""
    amount = clamp01(amount)
    if amount <= 0.001:
        return
    pitch, height, centre = keyboard_geometry(cx, top, width)
    cr.save()
    # grow out of Toby's hands: from 70% size, rising a few pixels
    k = 0.7 + 0.3 * amount
    cr.translate(cx, top + height / 2 + (1 - amount) * 6)
    cr.scale(k, k)
    cr.translate(-cx, -(top + height / 2))
    cr.push_group()
    _rounded_rect(cr, cx - width / 2 - pitch * 0.35, top, width + pitch * 0.7, height, pitch * 0.6)
    cr.set_source_rgb(0.13, 0.14, 0.25)
    cr.fill_preserve()
    cr.set_source_rgba(1, 1, 1, 0.16)
    cr.set_line_width(1)
    cr.stroke()
    size = pitch * 0.78
    for row in range(3):
        for col in range(10):
            kx, ky = centre(row, col)
            pressed = active == (row, col)
            _rounded_rect(cr, kx - size / 2, ky - size / 2 + (0.8 if pressed else 0), size, size, size * 0.25)
            if pressed:
                cr.set_source_rgb(*accent)
            else:
                cr.set_source_rgba(1, 1, 1, 0.2)
            cr.fill()
    sx, sy = centre(3, 0)
    pressed = active is not None and active[0] == 3
    _rounded_rect(cr, sx - pitch * 2.6, sy - size / 2 + (0.8 if pressed else 0), pitch * 5.2, size, size * 0.25)
    if pressed:
        cr.set_source_rgb(*accent)
    else:
        cr.set_source_rgba(1, 1, 1, 0.2)
    cr.fill()
    cr.pop_group_to_source()
    cr.paint_with_alpha(amount)
    cr.restore()


def _bent_limb(cr, x0, y0, x1, y1, segment, width, rgb, bend=1):
    """An arm from shoulder to hand with an elbow, so a hand closer than
    full reach bends the arm instead of the arm poking through it."""
    dx, dy = x1 - x0, y1 - y0
    d = math.hypot(dx, dy)
    if d < 1e-6 or d >= segment * 2:
        _limb(cr, x0, y0, x1, y1, width, rgb)
        return
    h = math.sqrt(max(0.0, segment * segment - (d / 2) ** 2))
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    # perpendicular, pointing away from the body (bend) and downward
    px, py = -dy / d * bend, dx / d * bend
    if py < 0:
        px, py = -px, -py
    ex, ey = mx + px * h, my + py * h
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    cr.set_line_width(width)
    cr.set_source_rgb(*rgb)
    cr.move_to(x0, y0)
    cr.line_to(ex, ey)
    cr.line_to(x1, y1)
    cr.stroke()


def _hand(cr, hx, hy, size, grip=False, press=0.0):
    """A round hand; closed around something when gripping."""
    cr.save()
    cr.translate(hx, hy)
    cr.scale(1 + press * 0.25, 1 - press * 0.25)
    cr.set_source_rgb(*SKIN_DARK)
    cr.arc(0, 0, size, 0, 2 * math.pi)
    cr.fill()
    if grip:
        # curled fingers: a short crease across the fist
        cr.set_source_rgba(*INK, 0.35)
        cr.set_line_width(size * 0.28)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.arc(0, 0, size * 0.55, math.pi * 1.15, math.pi * 1.85)
        cr.stroke()
    cr.restore()


def draw_chibi(cr, x, y, scale=1.0, pose="idle", phase=0.0, body=1.0,
               facing=1, look=(0.0, 0.0), reach=(0.0, -1.0), mouth=0.3,
               blink=0.0, squash=0.0, accent=(0.353, 0.549, 1.0), alpha=1.0,
               hand=None, stride=None, lean=0.0, keyboard=None, press=0.0):
    """Draw Toby with his feet at (x, y).

    body     0..1 — how grown the body is; 0 is just the floating head.
    phase    a running clock driving idle motion, talking and the sprout.
    reach    unit vector the reaching arm points along, when there's no hand.
    hand     an exact screen point for the facing hand (reaching, holding
             the pointer, pressing); the arm bends to get there.
    stride   walk-cycle position, advanced by distance walked, so the feet
             keep pace with the ground instead of sliding.
    lean     -1..1, leaning into the direction of travel.
    keyboard None, or {"amount": 0..1, "key": (row, col) or None,
             "tap": 0..1} for typing on the little keyboard.
    squash   tap reaction: positive squashes, negative stretches.
    """
    r = 30.0 * scale
    body = clamp01(body)
    grown = ease_out_back(body, 0.9) if body < 1 else 1.0
    walking = pose in ("walk", "carry")
    cycle_pos = stride if stride is not None else phase

    leg_len = r * 0.95 * grown
    torso_h = r * 1.05 * grown
    torso_w = r * 1.25 * max(0.25, grown)

    bob = 0.0
    if walking:
        bob = abs(math.sin(cycle_pos * 2 * math.pi)) * r * 0.10
    elif pose in ("idle", "talk", "think", "hold", "type"):
        bob = math.sin(phase * 2 * math.pi * 0.35) * r * 0.035
    elif pose == "cheer":
        bob = abs(math.sin(phase * 2 * math.pi * 0.9)) * r * 0.22

    sq = max(-0.25, min(0.25, squash))
    hip_y = y - leg_len - bob
    shoulder_y = hip_y - torso_h
    head_cy = shoulder_y - r * 0.78 * max(0.35, grown)
    if body <= 0.001:
        head_cy = y - r  # a floating head sits on the ground point

    angle = max(-1.0, min(1.0, lean)) * 0.13
    if hand is not None and angle:
        # the hand is a fixed point on screen; express it in the leaning frame
        hx0, hy0 = hand[0] - x, hand[1] - y
        c, s_ = math.cos(-angle), math.sin(-angle)
        hand = (x + hx0 * c - hy0 * s_, y + hx0 * s_ + hy0 * c)

    cr.save()
    cr.push_group()

    # contact shadow — shrinks as Toby hops up; it stays upright
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

    cr.translate(x, y)
    cr.rotate(angle)
    cr.translate(-x, -y)

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
            if walking:
                cycle = math.sin(cycle_pos * 2 * math.pi + (0 if side < 0 else math.pi))
                foot_lift = max(0.0, cycle) * leg_len * 0.28
                fx = hx + facing * cycle * r * 0.12
            else:
                foot_lift, fx = 0.0, hx
            fy = hip_y + leg_len - foot_lift
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

        # -- the keyboard, in front of the body, under the hands ------------
        kb = keyboard or {}
        kb_amount = clamp01(kb.get("amount", 0.0)) * clamp01(body)
        kb_width = r * 2.7
        kb_top = hip_y - torso_h * 0.4
        key_centre = None
        if kb_amount > 0.001:
            draw_keyboard(cr, x, kb_top, kb_width, kb_amount, kb.get("key"), accent)
            _p, _h, key_centre = keyboard_geometry(x, kb_top, kb_width)

        # -- arms -------------------------------------------------------------
        arm_len = r * 0.9 * grown
        segment = arm_len * 0.68
        for side in (-1, 1):
            sx = x + side * torso_w * 0.46
            sy = shoulder_y + r * 0.2
            gripping = False
            hand_press = 0.0
            exact = False
            if hand is not None and side == facing and pose in ("reach", "press", "hold", "carry"):
                hx, hy = hand
                exact = True
                gripping = pose in ("hold", "carry", "press")
                hand_press = press if pose == "press" else 0.0
            elif walking:
                cycle = math.sin(cycle_pos * 2 * math.pi + (math.pi if side < 0 else 0))
                hx = sx + side * r * (0.22 + 0.12 * max(0.0, cycle))
                hy = sy + arm_len * (0.95 - 0.18 * max(0.0, cycle))
            elif pose == "cheer":
                wave = math.sin(phase * 2 * math.pi * 1.6) * 0.25
                hx, hy = sx + side * arm_len * (0.45 + wave), sy - arm_len * 0.95
            elif pose == "type" and key_centre is not None:
                # each hand types its own half of the keyboard; the one whose
                # key is being pressed goes down onto it
                home = key_centre(1, 3 if side < 0 else 6)
                key = kb.get("key")
                mine = key is not None and ((key[0] == 3 and side < 0) or
                                            (key[0] < 3 and (key[1] < 5) == (side < 0)))
                if mine:
                    kx, ky = key_centre(*key)
                    hx, hy = kx, ky - r * 0.2 * (1 - clamp01(kb.get("tap", 0.0)))
                else:
                    hx, hy = home[0], home[1] - r * 0.2
                exact = True
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
            if exact:
                _bent_limb(cr, sx, sy, hx, hy, segment, limb_w, hoodie, bend=side)
            else:
                _limb(cr, sx, sy, hx, hy, limb_w, hoodie)
            _hand(cr, hx, hy, r * 0.16, gripping, hand_press)

    # -- head --------------------------------------------------------------
    cr.save()
    cr.translate(x, head_cy)
    cr.scale(1 + sq * 0.35, 1 - sq * 0.35)
    tilt = 0.06 * math.sin(phase * 2 * math.pi * 0.5) if pose in ("idle", "think", "talk") else 0.0
    if walking:
        tilt = 0.05 * facing
    elif pose == "type":
        tilt = 0.04 * math.sin(phase * 2 * math.pi * 0.4)   # reading along
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
    elif pose == "type":
        mouth = 0.1   # concentrating
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


def draw_ripple(cr, x, y, t, button="left", accent=(0.353, 0.549, 1.0)):
    """A click, made visible: a ring spreading from the pointer's tip.
    A right-click gets a second ring just behind the first."""
    rings = 2 if button == "right" else 1
    ease = EASE["enter"]
    cr.save()
    for i in range(rings):
        ti = (t - i * 0.18) / (1 - 0.18 * (rings - 1))
        if not 0.0 <= ti <= 1.0:
            continue
        radius = 4 + 22 * ease(ti)
        cr.new_path()
        cr.arc(x, y, radius, 0, 2 * math.pi)
        cr.set_line_width(2.4 * (1 - ti) + 0.8)
        cr.set_source_rgba(*accent, 0.75 * (1 - ti))
        cr.stroke()
        if i == 0 and ti < 0.4:
            cr.arc(x, y, 5, 0, 2 * math.pi)
            cr.set_source_rgba(*accent, 0.35 * (1 - ti / 0.4))
            cr.fill()
    cr.restore()


def draw_pen_cursor(cr, x, y, state="normal", phase=0.0, box=None, accent=(0.353, 0.549, 1.0)):
    """Toby's pen, with its nib on (x, y): the pointer of Work Mode.

    normal     the pen, resting
    thinking   a slow glow round the nib while Toby looks at the screen
    selecting  a soft outline round the thing about to be clicked
    drawing    the pen tilted in, nib touching
    clicking   a quick press
    dragging   pressed, with a faint trail
    """
    cr.save()
    if state == "thinking":
        pulse = 0.5 + 0.5 * math.sin(phase * 2 * math.pi * 1.1)
        grad = cairo.RadialGradient(x, y, 2, x, y, 26)
        grad.add_color_stop_rgba(0, *accent, 0.35 + 0.25 * pulse)
        grad.add_color_stop_rgba(1, *accent, 0)
        cr.set_source(grad)
        cr.arc(x, y, 26, 0, 2 * math.pi)
        cr.fill()
    if state == "selecting" and box is not None:
        bx, by, bw, bh = box
        _rounded_rect(cr, bx - 6, by - 5, bw + 12, bh + 10, 8)
        cr.set_source_rgba(*accent, 0.18)
        cr.fill_preserve()
        cr.set_source_rgba(*accent, 0.85)
        cr.set_line_width(2)
        cr.set_dash([6, 4])
        cr.stroke()
        cr.set_dash([])
    press = {"clicking": 3.0, "drawing": 1.5, "dragging": 2.0}.get(state, 0.0)
    tilt = -math.radians(38 if state in ("drawing", "dragging") else 45)   # body trails down-right
    cr.translate(x, y)
    cr.rotate(tilt)
    cr.translate(0, -press)
    # the pen body points away from the nib, down and to the right
    length, width = 44.0, 11.0
    cr.move_to(0, 0)
    cr.line_to(width / 2, 12)
    cr.line_to(-width / 2, 12)
    cr.close_path()
    cr.set_source_rgb(*INK)
    cr.fill()
    _rounded_rect(cr, -width / 2, 12, width, length - 12, width / 2)
    grad = cairo.LinearGradient(-width / 2, 0, width / 2, 0)
    grad.add_color_stop_rgb(0, *SKIN_LIGHT)
    grad.add_color_stop_rgb(1, *SKIN_DARK)
    cr.set_source(grad)
    cr.fill()
    cr.set_source_rgb(*accent)                  # the cap, in Toby's accent
    _rounded_rect(cr, -width / 2, length - 10, width, 10, width / 2)
    cr.fill()
    cr.set_source_rgba(1, 1, 1, 0.6)            # a highlight down one side
    cr.rectangle(-width / 2 + 2, 15, 2, length - 28)
    cr.fill()
    cr.restore()
    if state == "clicking":
        draw_ripple(cr, x, y, 0.35, "left", accent)


KEY_NAMES = {"ctrl": "Ctrl", "control": "Ctrl", "leftctrl": "Ctrl", "rightctrl": "Ctrl",
             "shift": "Shift", "leftshift": "Shift", "rightshift": "Shift",
             "alt": "Alt", "leftalt": "Alt", "rightalt": "Alt", "altgr": "AltGr",
             "super": "Super", "meta": "Super", "win": "Super", "cmd": "Super",
             "enter": "Enter", "return": "Enter", "esc": "Esc", "escape": "Esc",
             "tab": "Tab", "space": "Space", "backspace": "Backspace",
             "delete": "Del", "del": "Del", "up": "Up", "down": "Down",
             "left": "Left", "right": "Right", "home": "Home", "end": "End",
             "pageup": "PgUp", "pgup": "PgUp", "pagedown": "PgDn", "pgdn": "PgDn"}


def keycap_labels(combo):
    labels = []
    for part in str(combo).split("+"):
        part = part.strip()
        if part:
            labels.append(KEY_NAMES.get(part.lower(), part.upper() if len(part) == 1 else part.capitalize()))
    return labels


def draw_keycaps(cr, cx, bottom, labels, pressed, amount=1.0, accent=(0.353, 0.549, 1.0)):
    """A key combination as a row of keycaps, pressed one after another."""
    amount = clamp01(amount)
    if not labels or amount <= 0.001:
        return
    cr.save()
    cr.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    cr.set_font_size(12)
    widths = [max(30, cr.text_extents(l).x_advance + 18) for l in labels]
    gap = 6
    total = sum(widths) + gap * (len(widths) - 1)
    k = 0.8 + 0.2 * amount
    cr.translate(cx, bottom + (1 - amount) * 6)
    cr.scale(k, k)
    cr.push_group()
    x = -total / 2
    h = 28
    for label, w, down in zip(labels, widths, pressed):
        dy = 2 if down else 0
        # the key's side, visible when it's up
        _rounded_rect(cr, x, -h, w, h, 7)
        cr.set_source_rgb(0.08, 0.09, 0.16)
        cr.fill()
        _rounded_rect(cr, x, -h - 3 + dy, w, h, 7)
        if down:
            cr.set_source_rgb(*accent)
        else:
            cr.set_source_rgba(*SURFACE, 0.97)
        cr.fill_preserve()
        cr.set_source_rgba(*SURFACE_EDGE)
        cr.set_line_width(1)
        cr.stroke()
        cr.set_source_rgb(*INK_BRIGHT)
        tw = cr.text_extents(label).x_advance
        cr.move_to(x + (w - tw) / 2, -h / 2 + 1 + dy)
        cr.show_text(label)
        x += w + gap
    cr.pop_group_to_source()
    cr.paint_with_alpha(amount)
    cr.restore()


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

    Working the mouse is a sequence: walk to the pointer, take hold of it
    (hold), then follow it as it glides (the hold source is the glide's own
    plan, so the hand and the pointer can't drift apart), press it for a
    click, and let go before typing. Typing brings out a little keyboard
    whose keys light up in step with the text; key combinations appear as
    keycaps pressed in order.
    """

    HIDDEN, EMERGING, ACTIVE, RETURNING = "hidden", "emerging", "active", "returning"
    CHAR_SECONDS = 0.04        # about how fast ydotool types
    HOLD_PATIENCE = 1.5        # let go of a pointer nobody has moved for this long

    def __init__(self, settings, clock=time.monotonic):
        self.clock = clock
        self.settings = settings
        self.state = self.HIDDEN
        self.x = self.y = 0.0
        self.home = (0.0, 0.0)
        self.screen = None         # (width, height), to keep Toby on it
        self.target = None
        self.facing = 1
        self.pose = "idle"
        self.body = 0.0            # grown amount 0..1
        self.alpha = 0.0
        self.phase = 0.0
        self.stride = 0.0          # walk cycle, advanced by distance
        self.speed = 0.0           # current walking speed, px/s
        self.squash = Spring(0.0, stiffness=380, damping=18)
        self.lean = Spring(0.0, stiffness=140, damping=15)
        self.look = (0.0, 0.0)
        self.reach = (0.0, -1.0)
        self.blink = 0.0
        self._next_blink = 2.5
        self.bubble = ""
        self.title = ""
        self.steps = []
        self.ripples = []          # (x, y, started, button)
        self._arrival = None
        self._pose_until = None
        self._pose_after = "idle"
        self._reach_target = None
        self._hold = None          # () -> (x, y) of the pointer being held
        self._hold_side = 1
        self._hold_moved_at = 0.0
        self._press_until = 0.0
        self._typing = None        # {"text", "start", "end"}
        self._combo = None         # {"labels", "key", "start"}
        self._kb = (0.0, 0.0, 0.0, 0.0)   # keyboard: (from, to, start, seconds)
        self._glance_until = 0.0
        self._glance_at = None
        self._finish_at = None
        self._asking = False
        self.pen = None            # Work Mode's pen cursor: (x, y, state, box)
        self._pen_until = 0.0
        self._t_state = clock()
        self._last = clock()
        self._lock = threading.Lock()

    # -- geometry -----------------------------------------------------------------
    def stand_spot(self, hx, hy, side):
        """Where to stand so the hand reaches (hx, hy) from one side."""
        # the hand lands at the grip point; the feet go to one side, below
        fx, fy = hx + GRIP[0] + side * STAND[0], hy + GRIP[1] + STAND[1]
        if self.screen:
            w, h = self.screen
            fx = max(50.0, min(w - 50.0, fx))
            fy = max(140.0, min(h - 6.0, fy))
        return fx, fy

    def side_for(self, x):
        """Stand to the right of a point, unless that's off the screen."""
        if self.screen and x + STAND[0] + 60 > self.screen[0]:
            return -1
        if not self.screen and x > self.x + 1:
            return -1
        return 1

    # -- commands (called on the GTK thread) ------------------------------------
    def reload(self, settings):
        self.settings = settings

    def set_screen(self, width, height):
        self.screen = (float(width), float(height))

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
        """Update the checklist. Toby glances at it when a step is ticked
        off, and winces when one fails."""
        def count(status, items):
            return sum(1 for _l, st in items if st == status)
        before = self.steps
        self.title, self.steps = title, list(steps)
        if count("done", steps) > count("done", before):
            self._glance_at = (self.screen[0] - 160, 110) if self.screen else None
            self._glance_until = self.clock() + 0.45
            self.squash.kick(1.2)   # a small nod
        if count("error", steps) > count("error", before):
            self.oops()

    def walk_to(self, x, y, event=None, side=None):
        """Walk until the reaching hand is on (x, y). Returns an Event.

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
        if side is None:
            side = -1 if x > self.x else 1
        self._hold = None
        self.target = self.stand_spot(x, y, side)
        self._hold_side = side
        self._set_facing(-side if abs(self.target[0] - self.x) < 2 else
                         (1 if self.target[0] > self.x else -1))
        self._reach_target = (x, y)
        self.pose = "walk"
        distance = math.hypot(self.target[0] - self.x, self.target[1] - self.y)
        timeout = 0.4 + distance / max(100.0, self.settings.get("chibi_walk_speed", 900.0)) * 1.6
        self._arrival_deadline = self.clock() + min(3.0, timeout)
        return event

    def hold(self, source):
        """Take hold of the pointer. source() says where it is each frame."""
        self._hold = source
        self._hold_moved_at = self.clock()
        self.pose = "hold"
        self._pose_until = None
        self.target = None
        self.squash.kick(2.0)   # a little effort as the hand closes

    @property
    def holding(self):
        return self._hold is not None

    def release(self):
        """Let go of the pointer; the arm drops back to the side."""
        if self._hold is None:
            return
        self._hold = None
        self._reach_target = None
        if self.pose in ("hold", "carry", "press", "reach"):
            self.pose = "idle"
        self.squash.kick(-1.0)

    def click(self, button="left"):
        """Press the pointer: the hand pushes down and a ring spreads from
        the tip, a beat before the real click lands."""
        at = self._hold() if self._hold else self._reach_target
        if at is None:
            at = self.hand_position()
        self.ripples.append((at[0], at[1], self.clock(), button))
        self._press_until = self.clock() + DURATIONS["quick"]
        self.pose = "press"
        self.squash.kick(4.5)
        self._hold_moved_at = self.clock()

    def point_at(self, x, y, seconds=0.6):
        """Gesture toward a spot — where a window is about to open."""
        self.release()
        hx, hy = self.x, self.y - 110
        vx, vy = x - hx, y - hy
        n = math.hypot(vx, vy) or 1.0
        self.reach = (vx / n, vy / n)
        self._set_facing(1 if vx >= 0 else -1)
        self.look = (max(-1, min(1, vx / 300)), max(-1, min(1, vy / 300)))
        self.perform("reach", seconds)

    def begin_typing(self, text):
        self.release()
        now = self.clock()
        self._typing = {"text": str(text), "start": now + DURATIONS["quick"], "end": None}
        self._combo = None
        self._show_keyboard(True)
        self.pose = "type"
        self._pose_until = None

    def press_combo(self, combo):
        """Show a key combination being pressed, keycap by keycap."""
        self.release()
        labels = keycap_labels(combo)
        last = str(combo).split("+")[-1].strip()
        self._combo = {"labels": labels, "key": key_position(last) if len(last) == 1 else None,
                       "start": self.clock() + DURATIONS["quick"]}
        self._typing = None
        self._show_keyboard(True)
        self.pose = "type"
        self._pose_until = None

    def combo_seconds(self):
        """How long until every key in the combination is down."""
        if not self._combo:
            return 0.0
        return DURATIONS["quick"] + 0.07 * len(self._combo["labels"])

    def end_typing(self):
        """The keys are done: finish the text, and put the keyboard away."""
        if self._typing is not None and self._typing["end"] is None:
            self._typing["end"] = self.clock()
        self._show_keyboard(False)
        if self.pose == "type":
            self.perform("type", DURATIONS["standard"], then="idle")

    def ask_permission(self):
        """Waiting on the user to allow mouse and keyboard control."""
        self.release()
        self.bubble = "Can I use your mouse and keyboard?"
        self._asking = True
        self.pose = "think"
        self._pose_until = None
        self._glance_at = self.home
        self._glance_until = self.clock() + 30.0

    def oops(self):
        """A step failed: a small flinch, not a drama."""
        self.squash.kick(-3.5)
        self.perform("think", 0.6)

    def perform(self, pose, seconds=0.45, then="idle"):
        self.pose = pose
        self._pose_until = self.clock() + seconds
        self._pose_after = then
        if pose == "press":
            self.squash.kick(4.5)

    def say(self, text):
        self.bubble = text or ""
        if self._asking:
            # permission was given; stop looking back at the pill
            self._asking = False
            self._glance_until = 0.0
            if self.pose == "think" and self._pose_until is None:
                self.pose = "idle"

    def finish(self):
        """Cheer, then fold the body away and fly home."""
        self.release()
        self.bubble = ""
        self._typing = self._combo = None
        self._show_keyboard(False)
        self._glance_until = 0.0
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
        self._hold = None
        self._reach_target = None
        self._typing = self._combo = None
        self._kb = (0.0, 0.0, 0.0, 0.0)
        self.ripples = []
        self._finish_at = None
        self.speed = 0.0
        self.bubble = ""
        self._asking = False

    @property
    def visible(self):
        return self.state != self.HIDDEN

    def set_pen(self, x, y, state="normal", box=None):
        """Show Work Mode's pen cursor at (x, y). It stays a couple of
        seconds after the last move, then goes away by itself."""
        self.pen = (float(x), float(y), state, box)
        self._pen_until = self.clock() + 2.5

    def clear_pen(self):
        self.pen = None

    # -- state the stage draws -------------------------------------------------------
    def _set_facing(self, facing):
        if facing != self.facing:
            self.squash.kick(1.5)   # turning round hides the mirror flip
        self.facing = facing

    def _show_keyboard(self, show):
        now = self.clock()
        current = self.keyboard_amount(now)
        target = 1.0 if show else 0.0
        if self._kb[1] != target:
            self._kb = (current, target, now, DURATIONS["quick"] if show else DURATIONS["instant"] * 2)

    def keyboard_amount(self, now=None):
        start_value, target, t0, seconds = self._kb
        if seconds <= 0:
            return target
        t = clamp01(((now if now is not None else self.clock()) - t0) / seconds)
        ease = EASE["enter"] if target > start_value else EASE["exit"]
        return lerp(start_value, target, ease(t))

    def typed_count(self, now=None):
        """How many characters have been typed so far."""
        if not self._typing:
            return 0
        text = self._typing["text"]
        if self._typing["end"] is not None:
            return len(text)
        now = now if now is not None else self.clock()
        return max(0, min(len(text), int((now - self._typing["start"]) / self.CHAR_SECONDS)))

    def keyboard_state(self, now=None):
        """What draw_chibi needs for the keyboard, or None when it's away."""
        now = now if now is not None else self.clock()
        amount = self.keyboard_amount(now)
        if amount <= 0.001:
            return None
        key, tap = None, 0.0
        if self._typing and self._typing["end"] is None:
            text = self._typing["text"]
            progress = (now - self._typing["start"]) / self.CHAR_SECONDS
            if 0 <= progress < len(text):
                key = key_position(text[int(progress)])
                tap = max(0.0, 1.0 - (progress % 1.0) * 2.5)
        elif self._combo:
            caps = self.keycaps_state(now)
            if caps and all(caps[1]):
                key, tap = self._combo["key"], 1.0
        return {"amount": amount, "key": key, "tap": tap}

    def keycaps_state(self, now=None):
        """(labels, pressed flags, amount) for the combo keycaps, or None."""
        if not self._combo:
            return None
        now = now if now is not None else self.clock()
        labels, t0 = self._combo["labels"], self._combo["start"]
        n = len(labels)
        release = t0 + 0.07 * n + 0.22
        pressed = [t0 + 0.07 * i <= now < release for i in range(n)]
        appear = clamp01((now - (t0 - DURATIONS["quick"])) / DURATIONS["quick"])
        leave = clamp01((now - (release + 0.35)) / DURATIONS["standard"])
        amount = EASE["enter"](appear) * (1 - EASE["exit"](leave))
        if leave >= 1.0:
            self._combo = None
            return None
        return labels, pressed, amount

    def bubble_text(self, now=None):
        """The bubble: while typing, the text appearing as it's typed."""
        if self._typing:
            typed = self._typing["text"][: self.typed_count(now)]
            if len(typed) > 34:
                typed = "…" + typed[-33:]
            caret = "|" if self._typing["end"] is None and int(self.phase * 2.5) % 2 == 0 else " "
            return typed + caret
        if self._combo:
            return ""   # the keycaps say it
        return self.bubble

    def press_amount(self, now=None):
        now = now if now is not None else self.clock()
        left = self._press_until - now
        if left <= 0:
            return 0.0
        return math.sin(math.pi * (1 - left / DURATIONS["quick"]))

    # -- per frame ---------------------------------------------------------------
    def step(self):
        now = self.clock()
        dt = min(0.05, max(0.0, now - self._last))
        self._last = now
        self.phase += dt
        self.squash.step(dt)
        if self.pen is not None and now > self._pen_until:
            self.pen = None
        if self.state == self.HIDDEN:
            return self.pen is not None

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

        speed = self.settings.get("chibi_walk_speed", 900.0)
        moved_x = 0.0

        # walking
        if self.target is not None and self.state in (self.ACTIVE, self.EMERGING):
            tx, ty = self.target
            dx, dy = tx - self.x, ty - self.y
            dist = math.hypot(dx, dy)
            # speed up over the first fifth of a second, slow down near the end
            wanted = speed * min(1.0, 0.25 + dist / 220.0)
            self.speed = min(wanted, self.speed + speed * 5.0 * dt)
            step_len = self.speed * dt
            if dist <= max(2.0, step_len):
                self.x, self.y = tx, ty
                moved_x += dx
                self._advance_stride(dist, dt)
                self._arrive()
            else:
                self.x += dx / dist * step_len
                self.y += dy / dist * step_len
                moved_x += dx / dist * step_len
                self._advance_stride(step_len, dt)
                if abs(dx) > 30:
                    self._set_facing(1 if dx > 0 else -1)
        if self._arrival is not None and now >= getattr(self, "_arrival_deadline", now + 1):
            self._arrive()  # never let a caller wait forever

        # holding the pointer: the body follows the hand, the hand follows the
        # pointer's own glide plan, frame by frame
        if self._hold is not None and self.state == self.ACTIVE:
            at = None
            try:
                at = self._hold()
            except Exception:
                at = None
            if at is None:
                self.release()
            else:
                fx, fy = self.stand_spot(at[0], at[1], self._hold_side)
                d = math.hypot(fx - self.x, fy - self.y)
                moved_x += fx - self.x
                self.x, self.y = fx, fy
                self._reach_target = (at[0], at[1])
                self._set_facing(-self._hold_side)
                if d > 0.3:
                    self._hold_moved_at = now
                    self._advance_stride(d, dt)
                    if self.pose in ("hold", "carry"):
                        self.pose = "carry"
                elif self.pose == "carry":
                    self.pose = "hold"
                if now - self._hold_moved_at > self.HOLD_PATIENCE:
                    self.release()   # it's been left alone; the user may have taken the mouse back
        if self.pose == "press" and now >= self._press_until and self._pose_until is None:
            self.pose = "hold" if self._hold is not None else "reach"

        # leaning into the walk, and settling upright when stopping
        velocity = moved_x / dt if dt > 0 else 0.0
        self.lean.set_target(max(-1.0, min(1.0, velocity / max(100.0, speed))) * 0.9)
        self.lean.step(dt)

        # timed poses
        if self._pose_until is not None and now >= self._pose_until:
            self._pose_until = None
            self.pose = self._pose_after
            if self._hold is not None and self.pose == "idle":
                self.pose = "hold"
        if self._finish_at is not None and now >= self._finish_at:
            self._finish_at = None
            self._return_from = (self.x, self.y)
            self.state = self.RETURNING
            self._t_state = now
            self.target = None
        if self._typing and self._typing["end"] is not None and self.keyboard_amount(now) <= 0.001:
            self._typing = None

        self.ripples = [rp for rp in self.ripples if now - rp[2] < DURATIONS["gentle"]]

        # the eyes follow the thing being worked on, or glance at the checklist
        focus = self._reach_target
        if now < self._glance_until and self._glance_at is not None:
            focus = self._glance_at
        elif self._typing or self._combo:
            focus = (self.x, self.y - 40)   # looking down at the keys
        if focus is not None:
            hx, hy = self.x, self.y - 110
            vx, vy = focus[0] - hx, focus[1] - hy
            n = math.hypot(vx, vy) or 1.0
            if focus is self._reach_target:
                self.reach = (vx / n, vy / n)
            self.look = (max(-1, min(1, vx / 300)), max(-1, min(1, vy / 300)))
        return self.state != self.HIDDEN or self.pen is not None

    def _advance_stride(self, distance, dt):
        # one full cycle (two steps) per ~64px, but never faster than about
        # three and a half cycles a second: past that it's a scamper
        self.stride += min(distance / 64.0, dt * 3.5)

    def _arrive(self):
        self.target = None
        self.speed = 0.0
        if self.pose == "walk":
            self.pose = "reach"
        if self._reach_target is not None:
            self._set_facing(-self._hold_side)
        with self._lock:
            if self._arrival is not None:
                self._arrival.set()
                self._arrival = None

    def hand_position(self):
        """The point the reaching hand is on: while holding, the pointer."""
        rt = self._reach_target
        return rt if rt is not None else (self.x, self.y - 110)

    def draw_hand(self):
        """Where draw_chibi should put the facing hand, or None."""
        if self._reach_target is None or self.pose not in ("reach", "press", "hold", "carry"):
            return None
        rx, ry = self._reach_target
        return rx + GRIP[0], ry + GRIP[1]

    def bounds(self, scale=1.0):
        """A generous box around everything drawn, for partial redraws."""
        half_w, height = chibi_extent(scale)
        half_w = max(half_w, 160.0)   # the speech bubble is up to 290px wide
        x0, y0 = self.x - half_w - 20, self.y - height - 150
        x1, y1 = self.x + half_w + 20, self.y + 30
        points = [self._reach_target] if self._reach_target is not None else []
        points += [(rp[0], rp[1]) for rp in self.ripples]
        if self.pen is not None:
            px, py, _state, box = self.pen
            points += [(px, py), (px + 40, py + 40)]
            if box is not None:
                points += [(box[0] - 8, box[1] - 8), (box[0] + box[2] + 8, box[1] + box[3] + 8)]
            if self.state == self.HIDDEN:
                xs, ys = [p[0] for p in points], [p[1] for p in points]
                return (int(min(xs) - 34), int(min(ys) - 34),
                        int(max(xs) - min(xs) + 68), int(max(ys) - min(ys) + 68))
        for px, py in points:
            x0, y0 = min(x0, px - 34), min(y0, py - 34)
            x1, y1 = max(x1, px + 34), max(y1, py + 34)
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
            rects = [(x, y, w, h)]
            # The card only changes when a step does; between those, only its
            # pulsing marker moves, and a quarter of the frames is plenty for
            # that. Redrawing it every frame was most of the stage's cost.
            self._frame = getattr(self, "_frame", 0) + 1
            signature = (d.title, tuple(d.steps), round(d.alpha, 2))
            if signature != getattr(self, "_card_signature", None) or self._frame % 4 == 0:
                self._card_signature = signature
                rects.append(self._card_rect())
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
            accent = self._accent()
            if d.pen is not None:
                px, py, pstate, pbox = d.pen
                try:
                    draw_pen_cursor(cr, px, py, pstate, d.phase, pbox, accent)
                except Exception as e:
                    print("PEN CURSOR DRAW ERROR:", e, flush=True)
            if not d.visible:
                return False
            try:
                if d.steps:
                    cx, cy, _w, _h = self._card_rect()
                    draw_task_card(cr, cx, cy, d.title or "Working on it", d.steps,
                                   d.phase, d.alpha, accent)
                now = d.clock()
                for rx, ry, started, button in d.ripples:
                    draw_ripple(cr, rx, ry, (now - started) / DURATIONS["gentle"], button, accent)
                head_y = draw_chibi(cr, d.x, d.y, 1.0, d.pose, d.phase, d.body, d.facing,
                                    d.look, d.reach, blink=d.blink, squash=d.squash.value,
                                    accent=accent, alpha=d.alpha, hand=d.draw_hand(),
                                    stride=d.stride, lean=d.lean.value,
                                    keyboard=d.keyboard_state(now), press=d.press_amount(now))
                caps = d.keycaps_state(now)
                if caps:
                    labels, pressed, amount = caps
                    draw_keycaps(cr, d.x, head_y - 52, labels, pressed, amount * d.alpha, accent)
                text = d.bubble_text(now)
                if text.strip():
                    draw_bubble(cr, d.x, head_y - 44, text, d.alpha)
            except Exception as e:
                print("CHIBI DRAW ERROR:", e, flush=True)
                d.stop()
            return False

    return ChibiStage
