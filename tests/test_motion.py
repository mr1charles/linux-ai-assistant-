"""The shared motion engine: curves, the animator, reduced motion."""
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import toby_anim as ta  # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def check_true(label, got):
    if not got:
        failures.append(f"{label}: expected true, got {got!r}")


# -- the curves ---------------------------------------------------------------------
for name, ease in ta.EASE.items():
    check(f"{name} starts at 0", ease(0.0), 0.0)
    check(f"{name} ends at 1", ease(1.0), 1.0)
    samples = [ease(i / 100) for i in range(101)]
    check_true(f"{name} never goes backwards", all(b >= a - 1e-9 for a, b in zip(samples, samples[1:])))
    check_true(f"{name} never overshoots (no bounce anywhere in the system)",
               all(-1e-9 <= v <= 1 + 1e-9 for v in samples))

# the bezier solver against a known curve: CSS "ease" at x=0.5 is ~0.8024
css_ease = ta.cubic_bezier(0.25, 0.1, 0.25, 1.0)
check_true("the bezier solver matches CSS's own 'ease'", abs(css_ease(0.5) - 0.8024) < 0.001)
check_true("linear control points give a straight line",
           abs(ta.cubic_bezier(0.25, 0.25, 0.75, 0.75)(0.37) - 0.37) < 1e-4)
check_true("enter is front-loaded (most of the motion early)", ta.EASE["enter"](0.3) > 0.7)
check_true("exit is back-loaded (gathers, then goes)", ta.EASE["exit"](0.3) < 0.15)
check("css curve text", ta.css_curve("enter"), "cubic-bezier(0.16, 1.0, 0.3, 1.0)")
check("css duration text", ta.css_ms("emphasized"), "340ms")

# -- the animator -------------------------------------------------------------------------
clock = [100.0]
scheduled = []
anim = ta.Animator(lambda cb: scheduled.append(cb), clock=lambda: clock[0])
written = []


def run(seconds, fps=60):
    for _ in range(int(seconds * fps)):
        clock[0] += 1 / fps
        if scheduled and not scheduled[-1]():
            scheduled.pop()


done = []
anim.animate("x", 100, written.append, duration=0.3, start=0, on_done=lambda: done.append("x"))
check("it writes the start value at once", written[-1], 0.0)
check("and starts ticking", len(scheduled), 1)
run(0.15)
check_true("halfway through, it's past halfway (enter curve)", 50 < written[-1] < 100)
run(0.3)
check("it lands exactly on the target", written[-1], 100.0)
check("and reports finishing once", done, ["x"])
check("and stops ticking when nothing moves", scheduled, [])

# interrupting continues from where it is — no jump
anim.animate("x", 0, written.append, duration=0.3)
run(0.1)
here = written[-1]
anim.animate("x", 100, written.append, duration=0.3)
check_true("reversing mid-flight starts exactly where it was", abs(written[-1] - here) < 1e-6)
mark = len(written)
run(0.4)
after = written[mark - 1:]
reversal_step = max(abs(b - a) for a, b in zip(after, after[1:]))
# the fastest a fresh 0 -> 100 animation on the same curve ever moves in a frame
fresh = [100 * ta.EASE["enter"](i / 18) for i in range(19)]
fresh_step = max(b - a for a, b in zip(fresh, fresh[1:]))
check_true("after reversing, nothing moves faster than a fresh animation would",
           reversal_step <= fresh_step + 1e-6)
check("it still arrives", written[-1], 100.0)

# a superseded animation never runs its on_done
cancelled = []
anim.animate("y", 1, lambda v: None, duration=0.3, start=0, on_done=lambda: cancelled.append("hide"))
run(0.1)
anim.animate("y", 0, lambda v: None, duration=0.3)
run(0.5)
check("an interrupted animation doesn't finish its old job", cancelled, [])

# delays hold the start value, then run
vals = []
anim.animate("d", 1, vals.append, duration=0.2, start=0, delay=0.2)
run(0.15)
check_true("during the delay it stays put", all(v == 0 for v in vals))
run(0.4)
check("after the delay it arrives", vals[-1], 1.0)

# time-based: fewer frames don't make it longer
clock2 = [0.0]
sched2 = []
slow = ta.Animator(lambda cb: sched2.append(cb), clock=lambda: clock2[0])
end = []
slow.animate("z", 1, lambda v: None, duration=0.3, start=0, on_done=lambda: end.append(clock2[0]))
while sched2:
    clock2[0] += 0.1              # a struggling machine: 10 frames a second
    if not sched2[-1]():
        sched2.pop()
check_true("a slow frame rate doesn't stretch the animation", end and end[0] <= 0.31)

# reduced motion: straight to the end, and still reports done
calm = ta.Animator(lambda cb: failures.append("reduced motion scheduled a frame"),
                   clock=lambda: 0.0, reduce_motion=lambda: True)
out, fin = [], []
calm.animate("r", 5, out.append, duration="gentle", start=0, on_done=lambda: fin.append(1))
check("reduced motion jumps to the end", out, [5])
check("and still reports finishing", fin, [1])

# a failing apply doesn't break the engine
boom = ta.Animator(lambda cb: sched2.append(cb), clock=lambda: clock2[0])
import io, contextlib  # noqa: E401
_quiet = contextlib.redirect_stdout(io.StringIO())
_quiet.__enter__()
boom.animate("bad", 1, lambda v: 1 / 0, duration=0.1, start=0)
while sched2:
    clock2[0] += 0.02
    if not sched2[-1]():
        sched2.pop()
_quiet.__exit__(None, None, None)
check("an exception in one animation doesn't stop the engine", boom.is_running("bad"), False)

# settings
check("the master switch also turns on reduced motion",
      ta.animation_settings({"animations": {"enabled": False}})["reduce_motion"], True)

if failures:
    print(f"{len(failures)} PROBLEM(S):")
    for f in failures:
        print("  ", f)
    sys.exit(1)
print("motion engine checks passed")
