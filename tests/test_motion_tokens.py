"""One motion language, checked everywhere it's used.

Toby's curves and durations are defined once, in toby_anim.py. This makes
sure the four places that consume them — Python animations, the GTK
stylesheet, Hyprland's window animations and the phone app — really do use
them, and that nobody reintroduces a private timer loop or easing function.
"""
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import hypr_animations  # noqa: E402
import toby_anim  # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def read(*parts):
    return open(os.path.join(REPO, *parts)).read()


# -- Hyprland uses the same curves ---------------------------------------------------
check("Hyprland arrivals use 'enter'", hypr_animations.BEZIERS["tobyOut"], toby_anim.CURVES["enter"])
check("Hyprland departures use 'exit'", hypr_animations.BEZIERS["tobyIn"], toby_anim.CURVES["exit"])
check("Hyprland movement uses 'move'", hypr_animations.BEZIERS["tobyMove"], toby_anim.CURVES["move"])

# -- the phone app declares the same tokens ------------------------------------------
css = read("mobile", "app.css")
for name in toby_anim.CURVES:
    m = re.search(rf"--curve-{name}:\s*([^;]+);", css)
    check(f"phone --curve-{name}", m and m.group(1).strip(), toby_anim.css_curve(name))
for name in toby_anim.DURATIONS:
    m = re.search(rf"--dur-{name}:\s*([^;]+);", css)
    check(f"phone --dur-{name}", m and m.group(1).strip(), toby_anim.css_ms(name))
for line in css.splitlines():
    if "transition:" in line and re.search(r"\d+ms", line):
        failures.append(f"phone transition with a private duration: {line.strip()}")

# -- the GTK stylesheet transitions use the tokens ------------------------------------
app_src = read("scripts", "linux_agent_apple.py")
for line in app_src.splitlines():
    if "transition:" in line and ("ease-out" in line or re.search(r"\d+ms", line)):
        failures.append(f"stylesheet transition with a private timing: {line.strip()}")

# -- no private animation machinery left behind ---------------------------------------
for pattern, why in [
    (r"^def ease_", "an easing function defined outside toby_anim"),
    (r"GLib\.timeout_add\(14,", "a hand-written 14 ms animation loop"),
    (r'state\["i"\] \+= 1', "a step-counting animation (slows down on a busy machine)"),
    (r"1\.70158", "the classic overshooting 'back' curve"),
]:
    if re.search(pattern, app_src, re.M):
        failures.append(f"linux_agent_apple.py still has {why}")
if "_GLIDE_STEPS" in read("scripts", "screen_control.py"):
    failures.append("the mouse glide still counts steps instead of using time")

if failures:
    print(f"{len(failures)} PROBLEM(S):")
    for f in failures:
        print("  ", f)
    sys.exit(1)
print("motion token checks passed")
