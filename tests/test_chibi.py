"""Checks on the chibi's motion and timing, and on its drawing.

The important property: the task runner waits for the chibi to reach each
target before moving the real pointer, on a background thread. If an
animation could stall that wait, a task would hang. So every wait must end
by itself, and stopping the chibi must release every waiter at once.
"""
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import cairo  # noqa: E402

import chibi  # noqa: E402
import toby_anim  # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def check_true(label, got):
    if not got:
        failures.append(f"{label}: expected true, got {got!r}")


class Clock:
    def __init__(self):
        self.now = 50.0

    def __call__(self):
        return self.now


def frames(director, clock, seconds, fps=60):
    for _ in range(int(seconds * fps)):
        clock.now += 1 / fps
        director.step()


settings = toby_anim.animation_settings({})

# -- emerging grows the body out of the pill --------------------------------
clock = Clock()
d = chibi.ChibiDirector(settings, clock=clock)
check("starts hidden", d.visible, False)
d.emerge(900, 1000, "Opening your apps", [("Open Firefox", "current")])
check("emerging makes it visible", d.visible, True)
frames(d, clock, 0.1)
check_true("partway through emerging the body is partway grown", 0.0 < d.body < 1.0)
frames(d, clock, 0.6)
check("after the transform it is fully grown", d.body, 1.0)
check("and active", d.state, d.ACTIVE)
check_true("it rose up out of the pill", d.y < 1000)

# -- walking arrives, and the hand lands on the target -----------------------
arrived = d.walk_to(300, 400)
check("walking uses the walk pose", d.pose, "walk")
check_true("not arrived straight away", not arrived.is_set())
frames(d, clock, 2.5)
check_true("arrives within a sensible time", arrived.is_set())
check("the hand is exactly on the target", d.hand_position(), (300, 400))
check_true("the body stands beside the target, not on top of it", abs(d.x - 300) > 20)
check("on arrival it reaches for the target", d.pose, "reach")

# -- a wait can never hang ----------------------------------------------------
stuck = d.walk_to(5000, 5000)          # somewhere far away
clock.now += 10                         # ...and no frames arrive at all
d.step()
check_true("a walk with no frames still releases its waiter by the deadline", stuck.is_set())

waiter = d.walk_to(100, 100)
d.stop()
check_true("stopping releases anyone waiting", waiter.is_set())
check("stopping hides it", d.visible, False)

# a real background thread waiting, as the task runner does
clock = Clock()
d = chibi.ChibiDirector(settings, clock=clock)
d.emerge(500, 900, "x", [])
done = threading.Event()


def runner():
    event = d.walk_to(1200, 200)
    event.wait(timeout=5)
    done.set()


t = threading.Thread(target=runner)
t.start()
for _ in range(400):
    clock.now += 1 / 60
    d.step()
    if done.is_set():
        break
t.join(timeout=2)
check_true("a background runner is released when the chibi arrives", done.is_set())

# -- a second walk supersedes the first ---------------------------------------
first = d.walk_to(10, 10)
second = d.walk_to(20, 20)
check_true("starting a new walk releases the previous waiter", first.is_set())
check_true("the new walk is still pending", not second.is_set())

# -- timed poses revert --------------------------------------------------------
d.perform("press", 0.3)
check("press pose shows", d.pose, "press")
frames(d, clock, 0.5)
check("a timed pose goes back to idle", d.pose, "idle")

# -- finishing cheers, returns home, and hides ----------------------------------
clock = Clock()
d = chibi.ChibiDirector(settings, clock=clock)
d.emerge(700, 1000, "x", [("a", "done")])
frames(d, clock, 0.7)
d.walk_to(200, 200)
frames(d, clock, 2.0)
d.finish()
check("finishing cheers first", d.pose, "cheer")
frames(d, clock, 0.4)
check_true("still out while cheering", d.visible)
frames(d, clock, 2.0)
check("then it goes home and disappears", d.visible, False)
check("with its body folded away", d.body, 0.0)

# -- the dirty rectangle covers everything drawn --------------------------------
clock = Clock()
d = chibi.ChibiDirector(settings, clock=clock)
d.emerge(600, 700, "x", [])
frames(d, clock, 0.8)
x, y, w, h = d.bounds()
surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1400, 1000)
cr = cairo.Context(surface)
chibi.draw_chibi(cr, d.x, d.y, 1.0, "cheer", 0.2, 1.0)
surface.flush()
data = surface.get_data()
stride = surface.get_stride()
outside = 0
for py in range(0, 1000, 3):
    for px in range(0, 1400, 3):
        if data[py * stride + px * 4 + 3] and not (x <= px < x + w and y <= py < y + h):
            outside += 1
check("nothing is drawn outside the area that gets redrawn", outside, 0)

# -- working the mouse: hold, carry, press, let go -------------------------------
clock = Clock()
d = chibi.ChibiDirector(settings, clock=clock)
d.set_screen(1600, 1000)
d.emerge(800, 980, "x", [])
frames(d, clock, 0.7)
pointer = [400.0, 300.0]
arrived = d.walk_to(*pointer, side=d.side_for(1200))
frames(d, clock, 2.5)
check_true("walked to the pointer", arrived.is_set())
d.hold(lambda: tuple(pointer))
check("holding it", (d.holding, d.pose), (True, "hold"))
check("the hand is on the pointer", d.hand_position(), (400.0, 300.0))
hx, hy = d.draw_hand()
check("and grips its tail, leaving the tip uncovered", (hx - 400, hy - 300), chibi.GRIP)
stride_before = d.stride
for i in range(30):                      # the pointer glides; Toby follows it
    pointer[0] += 20
    clock.now += 1 / 60
    d.step()
check("carrying uses the walking pose", d.pose, "carry")
check("the hand stayed on the pointer the whole way", d.hand_position(), (1000.0, 300.0))
check_true("the legs walked while carrying", d.stride > stride_before)
check_true("he leans into the carry", d.lean.value > 0.1)
frames(d, clock, 0.3)
check("standing still again, still holding", d.pose, "hold")
frames(d, clock, 1.0)
check_true("upright again after stopping", abs(d.lean.value) < 0.1)

# a step's worth of walking moves the feet a step, not a blur: the walk cycle
# follows distance, so a short shuffle is a short shuffle
d2 = chibi.ChibiDirector(settings, clock=Clock())
d2._advance_stride(32.0, 1.0)
check("half a cycle per 32px", round(d2.stride, 3), 0.5)
d2._advance_stride(5000.0, 1 / 60)
check_true("but never faster than a scamper", d2.stride < 0.6)

d.click("left")
check("clicking presses", d.pose, "press")
check("and a ring spreads from the pointer", [(r[0], r[1]) for r in d.ripples], [(1000.0, 300.0)])
x, y, w, h = d.bounds()
check_true("the ring is inside the redrawn area", x <= 1000 - 26 and x + w >= 1000 + 26 and y <= 300 - 26)
frames(d, clock, 0.3)
check("after the press it's holding again", d.pose, "hold")
frames(d, clock, 0.4)
check("the ring is gone once it has faded", d.ripples, [])
frames(d, clock, 2.0)
check("left alone for a while, he lets go (the user may have the mouse)", d.holding, False)

# the pointer source failing never breaks a frame
d.hold(lambda: 1 / 0)
d.step()
check("a failing pointer source just lets go", d.holding, False)

# -- typing: the keyboard comes out, the text appears as it's typed -------------
d.hold(lambda: (500.0, 500.0))
d.begin_typing("hello")
check("typing lets go of the pointer", d.holding, False)
check("and uses the typing pose", d.pose, "type")
check("the bubble is empty before the first key", d.bubble_text().strip("| "), "")
frames(d, clock, 0.16)
check_true("the keyboard is out", d.keyboard_state()["amount"] > 0.9)
frames(d, clock, 0.09)
typed = d.bubble_text().rstrip("| ")
check_true(f"letters appear one by one (got {typed!r})", 0 < len(typed) < 5 and "hello".startswith(typed))
state = d.keyboard_state()
check("the key being typed is lit", state["key"], chibi.key_position("hello"[len(typed)]))
d.end_typing()
check("finishing shows the whole text", d.bubble_text().rstrip("| "), "hello")
frames(d, clock, 0.6)
check("the keyboard is put away", d.keyboard_state(), None)
check("and he's back to idle", d.pose, "idle")

check("letters map to the keyboard", chibi.key_position("q"), (0, 0))
check("so do digits", chibi.key_position("0"), (0, 9))
check("space is the space bar", chibi.key_position(" "), (3, 4))
check("anything else lights nothing", chibi.key_position("é"), None)

# -- key combinations as keycaps --------------------------------------------------
check("combos get readable keycaps", chibi.keycap_labels("ctrl+shift+t"), ["Ctrl", "Shift", "T"])
check("named keys are spelled out, no symbols", chibi.keycap_labels("super+left"), ["Super", "Left"])
d.press_combo("ctrl+w")
frames(d, clock, 0.17)
labels, pressed, amount = d.keycaps_state()
check("the first key goes down first", pressed, [True, False])
frames(d, clock, d.combo_seconds() - 0.17 + 0.01)
check("then both are down when the press lands", d.keycaps_state()[1], [True, True])
check("with the last key lit on the keyboard", d.keyboard_state()["key"], chibi.key_position("w"))
check("the keycaps replace the bubble", d.bubble_text(), "")
d.end_typing()
frames(d, clock, 1.2)
check("the keycaps go away", d.keycaps_state(), None)

# -- asking first, then getting on with it ---------------------------------------
d.hold(lambda: (500.0, 500.0))
d.ask_permission()
check("asking lets go of the pointer", d.holding, False)
check("and asks in the bubble", "mouse" in d.bubble_text(), True)
frames(d, clock, 0.1)
check_true("looking back toward the pill", d.look[1] > 0.3)
d.say("Move the pointer")
check("once allowed, the question goes", d.bubble_text(), "Move the pointer")
check("and he stops looking back", d._glance_until, 0.0)

# -- a finished step gets a glance at the checklist ------------------------------
d.set_steps("x", [("a", "current")])
d.set_steps("x", [("a", "done")])
frames(d, clock, 0.1)
check_true("he glances up at the card", d.look[1] < 0 and d.look[0] > 0)

# -- stopping clears everything, so nothing lingers next time ---------------------
d.hold(lambda: (1.0, 1.0))
d.click()
d.begin_typing("x")
d.stop()
check("stop drops the pointer, the keyboard and the rings",
      (d.holding, d.keyboard_state(), d.ripples, d.bubble_text().strip("| ")), (False, None, [], ""))

# -- drawing: every pose renders, and the checklist marks what's done ------------
workdir = tempfile.mkdtemp()
for pose in chibi.POSES:
    for extra in ({}, {"hand": (110, 150), "lean": 0.7, "stride": 0.4, "press": 0.5},
                  {"keyboard": {"amount": 0.6, "key": (1, 3), "tap": 0.5}},
                  {"hand": (150, 180)}):   # a hand closer than full reach bends the elbow
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 300, 300)
        c = cairo.Context(surf)
        try:
            chibi.draw_chibi(c, 150, 260, 1.0, pose, 0.3, 1.0, reach=(0.5, -0.8), **extra)
        except Exception as e:
            failures.append(f"pose {pose} {sorted(extra)} failed to draw: {e!r}")
surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 300, 300)
c = cairo.Context(surf)
for t in (0.0, 0.3, 0.99):
    chibi.draw_ripple(c, 150, 150, t, "right")
chibi.draw_keycaps(c, 150, 100, ["Ctrl", "Shift", "T"], [True, True, False])
chibi.draw_keycaps(c, 150, 100, [], [])     # nothing to show draws nothing

# the held hand really is where it's asked to be, even leaning
surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 300, 300)
c = cairo.Context(surf)
chibi.draw_chibi(c, 190, 260, 1.0, "hold", 0.0, 1.0, facing=-1, hand=(149, 182), lean=0.8)
surf.flush()
data, stride = surf.get_data(), surf.get_stride()
px = data[182 * stride + 149 * 4: 182 * stride + 149 * 4 + 4]
check_true("a hand is drawn exactly at the grip point", px[3] > 200)

steps = [("Open Firefox", "done"), ("Type the search", "current"), ("Press Enter", "pending")]
w, h = chibi.task_card_size("Doing it", steps)
check_true("the card grows with the number of steps",
           chibi.task_card_size("x", steps + steps)[1] > h)
surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 400, 300)
c = cairo.Context(surf)
chibi.draw_task_card(c, 10, 10, "Doing it", steps)
surf.write_to_png(os.path.join(workdir, "card.png"))

# a very long label is shortened rather than running off the card
surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 400, 300)
c = cairo.Context(surf)
chibi.draw_task_card(c, 0, 0, "x", [("word " * 60, "pending")])
surf.flush()
data, stride = surf.get_data(), surf.get_stride()
spill = any(data[row * stride + col * 4 + 3] for row in range(0, 300) for col in range(chibi.CARD_WIDTH + 12, 400))
check("a long label stays inside the card", spill, False)

chibi.draw_bubble(c, 200, 280, "")   # empty text draws nothing and doesn't fail

if failures:
    print(f"{len(failures)} PROBLEM(S):")
    for f in failures:
        print("  ", f)
    sys.exit(1)
print("chibi checks passed")
