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

# -- drawing: every pose renders, and the checklist marks what's done ------------
workdir = tempfile.mkdtemp()
for pose in chibi.POSES:
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 300, 300)
    c = cairo.Context(surf)
    try:
        chibi.draw_chibi(c, 150, 260, 1.0, pose, 0.3, 1.0, reach=(0.5, -0.8))
    except Exception as e:
        failures.append(f"pose {pose} failed to draw: {e!r}")

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
