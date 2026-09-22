"""Checks on the lid-fold state machine.

The one thing this feature must never do is get between the laptop and
sleep. These drive FoldController with a fake clock, a fake view and a fake
sleep lock through everything the brief listed — rapid close/open, suspend
arriving early, the animation crashing, a screenshot that can't be taken —
and check two things every time: the lock is released within the cap, and
the overlay is gone once it's all over.
"""
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import toby_fold as tf  # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def check_true(label, got):
    if not got:
        failures.append(f"{label}: expected true, got {got!r}")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class View:
    def __init__(self, fail_on_draw=False, fail_on_show=False):
        self.visible = False
        self.draws = 0
        self.fail_on_draw = fail_on_draw
        self.fail_on_show = fail_on_show

    def show(self, source):
        if self.fail_on_show:
            raise RuntimeError("no compositor")
        self.visible = True

    def hide(self):
        self.visible = False

    def redraw(self):
        if self.fail_on_draw:
            raise RuntimeError("draw exploded")
        self.draws += 1


class Lock:
    def __init__(self):
        self.held = True
        self.released_at = None
        self.acquisitions = 0

    def acquire(self):
        self.held = True
        self.acquisitions += 1

    def release(self):
        self.held = False


def make(capture=lambda: "screenshot", settings=None, view=None):
    clock = Clock()
    view = view or View()
    lock = Lock()
    controller = tf.FoldController(view, lock, capture, settings or {}, clock=clock)
    return controller, clock, view, lock


def run_frames(controller, clock, seconds, fps=60):
    for _ in range(int(seconds * fps)):
        clock.advance(1.0 / fps)
        controller.tick()


# -- the ordinary close ------------------------------------------------------
c, clock, view, lock = make()
c.on_sleep()
check("closing shows the overlay", view.visible, True)
check("closing starts folding", c.state, c.FOLDING)
check_true("the lock is still held while folding", lock.held)
run_frames(c, clock, 0.3)
check_true("partway through, progress is partway", 0.2 < c.progress < 0.8)
run_frames(c, clock, 0.5)
check("after the close duration it is folded", c.state, c.FOLDED)
check("folded means fully closed", c.progress, 1.0)
check("the lock is released once folded", lock.held, False)
check("the overlay stays up (black) through the suspend", view.visible, True)

# -- and the ordinary open ---------------------------------------------------
c.on_resume()
check("resuming re-takes the lock for next time", lock.held, True)
check("resuming unfolds", c.state, c.UNFOLDING)
run_frames(c, clock, 0.4)
check_true("partway through opening, progress is partway", 0.0 < c.progress < 1.0)
run_frames(c, clock, 0.6)
check("opening finishes back at idle", c.state, c.IDLE)
check("the overlay is gone afterwards", view.visible, False)
check("the screenshot is dropped afterwards", c.source, None)

# -- the lock is always released within the cap, even with no frames at all -
c, clock, view, lock = make()
c.on_sleep()
clock.advance(tf.HARD_RELEASE_CAP_S + 0.01)
c.tick()   # a single late frame, as if the compositor stalled
check("a stalled animation still releases the lock by the deadline", lock.held, False)

# -- a failed screenshot means no animation and no delay ---------------------
c, clock, view, lock = make(capture=lambda: None)
c.on_sleep()
check("no screenshot: lock released immediately", lock.held, False)
check("no screenshot: nothing shown", view.visible, False)
check("no screenshot: stays idle", c.state, c.IDLE)


def exploding_capture():
    raise OSError("grim crashed")


c, clock, view, lock = make(capture=exploding_capture)
c.on_sleep()
check("a crashing screenshot tool still releases the lock at once", lock.held, False)
check("a crashing screenshot tool leaves nothing on screen", view.visible, False)

# -- the overlay failing to appear, or failing to draw -----------------------
c, clock, view, lock = make(view=View(fail_on_show=True))
c.on_sleep()
check("an overlay that can't show releases the lock at once", lock.held, False)
check("and leaves the controller idle", c.state, c.IDLE)

c, clock, view, lock = make(view=View(fail_on_draw=True))
c.on_sleep()
clock.advance(1 / 60)
keep = c.tick()
check("a crash while drawing stops the animation", keep, False)
check("a crash while drawing releases the lock", lock.held, False)
check("a crash while drawing takes the overlay down", view.visible, False)

c, clock, view, lock = make()
c.on_sleep()
c.draw_failed(RuntimeError("cairo error"))
check("a draw failure reported by the view releases the lock", lock.held, False)
check("a draw failure reported by the view hides it", view.visible, False)

# -- rapid close/open: reverse from wherever we are, no jumps ----------------
c, clock, view, lock = make()
c.on_sleep()
run_frames(c, clock, 0.25)
halfway = c.progress
c.on_resume()                       # opened again before it finished closing
check("opening mid-fold turns around into an unfold", c.state, c.UNFOLDING)
clock.advance(1 / 60)
c.tick()
check_true("turning around doesn't jump", abs(c.progress - halfway) < 0.05)
run_frames(c, clock, 0.1)
before = c.progress
c.on_sleep()                        # and closed again mid-unfold
check("closing mid-unfold turns around into a fold", c.state, c.FOLDING)
clock.advance(1 / 60)
c.tick()
check_true("that turnaround doesn't jump either", abs(c.progress - before) < 0.05)
check_true("progress always stays within 0..1", 0.0 <= c.progress <= 1.0)

# ten fast cycles in a row
c, clock, view, lock = make()
for _ in range(10):
    c.on_sleep()
    run_frames(c, clock, 0.07)
    c.on_resume()
    run_frames(c, clock, 0.05)
run_frames(c, clock, 2.0)
check("after ten rapid cycles it settles back to idle", c.state, c.IDLE)
check("after ten rapid cycles the overlay is gone", view.visible, False)
check("after ten rapid cycles the lock is held, ready for next time", lock.held, True)

# -- suspend arrives before the animation has finished ------------------------
c, clock, view, lock = make()
c.on_sleep()
run_frames(c, clock, 0.2)
# the machine goes to sleep here; the monotonic clock doesn't advance
c.on_resume()
run_frames(c, clock, 1.5)
check("an interrupted fold unfolds cleanly after resume", c.state, c.IDLE)
check("an interrupted fold leaves nothing on screen", view.visible, False)

# -- suspend that never comes -------------------------------------------------
c, clock, view, lock = make()
c.on_sleep()
run_frames(c, clock, 1.0)
check("folded, waiting for a suspend", c.state, c.FOLDED)
run_frames(c, clock, tf.FOLDED_WATCHDOG_S + 1.0)
check_true("a suspend that never happens doesn't leave the screen black",
           c.state in (c.UNFOLDING, c.IDLE))
run_frames(c, clock, 1.5)
check("and the desktop comes back", view.visible, False)

# -- disabled ------------------------------------------------------------------
c, clock, view, lock = make(settings={"animations": {"fold_enabled": False}})
c.on_sleep()
check("with the fold turned off, the lock is released at once", lock.held, False)
check("with the fold turned off, nothing is shown", view.visible, False)

c, clock, view, lock = make(settings={"animations": {"enabled": False}})
c.on_sleep()
check("the master animation switch turns the fold off too", lock.held, False)

# -- preview never gives up the lock -----------------------------------------
c, clock, view, lock = make()
c.preview()
run_frames(c, clock, 2.0)
check("a preview plays through and finishes", c.state, c.IDLE)
check("a preview keeps the sleep lock the whole time", lock.held, True)

# ...but a real suspend during a preview still gets its lock on time
c, clock, view, lock = make()
c.preview()
run_frames(c, clock, 0.2)
c.on_sleep()
run_frames(c, clock, 1.0)
check("a real suspend mid-preview still releases the lock", lock.held, False)

# -- shutdown ------------------------------------------------------------------
c, clock, view, lock = make()
c.on_shutdown()
check("shutting down fades rather than folds", c.state, c.FADING)
run_frames(c, clock, 1.0)
check("the shutdown fade releases the lock", lock.held, False)
c.on_resume()   # shutdown cancelled
run_frames(c, clock, 1.5)
check("a cancelled shutdown restores the desktop", view.visible, False)

# -- monitor choice ----------------------------------------------------------
check("the laptop panel is preferred",
      tf.pick_monitor([{"name": "HDMI-A-1", "focused": True}, {"name": "eDP-1"}])["name"], "eDP-1")
check("without a laptop panel, the focused one",
      tf.pick_monitor([{"name": "DP-1"}, {"name": "DP-2", "focused": True}])["name"], "DP-2")
check("no monitors, no choice", tf.pick_monitor([]), None)

if failures:
    print(f"{len(failures)} PROBLEM(S):")
    for f in failures:
        print("  ", f)
    sys.exit(1)
print("lid fold checks passed")
