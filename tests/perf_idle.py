"""Guard against Toby quietly burning CPU while doing nothing.

Measures the app's own CPU time over a few seconds, hidden and with the
pill open but idle. Before this was tuned, an open idle pill cost ~18% of a
core (a heavy CSS shadow re-blurred at 60 fps); it's now a few percent.
The limits have generous headroom so only a real regression trips them.
"""
import os
import resource
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _layer_shell_stub  # noqa: E402

_layer_shell_stub.install()
from gi.repository import GLib  # noqa: E402

import linux_agent_apple as app  # noqa: E402

app.SETTINGS["startup_greeting"] = False
LIMITS = {"hidden": 3.0, "open and idle": 10.0}

win = app.AssistantWindow(app.RingFlash())
ctx = GLib.MainContext.default()


def spin(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ctx.iteration(True)


def measure(seconds=5):
    r0 = resource.getrusage(resource.RUSAGE_SELF)
    t0 = time.monotonic()
    spin(seconds)
    r1 = resource.getrusage(resource.RUSAGE_SELF)
    cpu = (r1.ru_utime - r0.ru_utime) + (r1.ru_stime - r0.ru_stime)
    return 100.0 * cpu / (time.monotonic() - t0)


failures = []
spin(1.5)
hidden = measure()
win.show_panel()
spin(1.5)
open_idle = measure()
for label, value in (("hidden", hidden), ("open and idle", open_idle)):
    print(f"{label:<14} {value:4.1f}% of one core (limit {LIMITS[label]}%)")
    if value > LIMITS[label]:
        failures.append(f"{label}: {value:.1f}% is over {LIMITS[label]}%")
if failures:
    print("PROBLEM(S):", "; ".join(failures))
    sys.exit(1)
print("idle CPU checks passed")
