"""Work Mode reads controls from the accessibility tree.

Starts a real GTK window (tests/fixtures/a11y_window.py) whose buttons have
white text on blue, which OCR can't read, and checks vision finds them by
name, with their real on-screen boxes, through AT-SPI. Needs a display,
a session bus and at-spi2-core; run as
    dbus-run-session -- xvfb-run -a python3 tests/test_accessibility.py
Skipped (with a note) where those aren't available.
"""
import os
import shutil
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import vision  # noqa: E402

launcher = next((p for p in ("/usr/libexec/at-spi-bus-launcher", "/usr/lib/at-spi2-core/at-spi-bus-launcher",
                             "/usr/lib/at-spi-bus-launcher") if os.path.exists(p)), None)
if not os.environ.get("DISPLAY") or not os.environ.get("DBUS_SESSION_BUS_ADDRESS") or not launcher:
    print("skipped: needs a display, a session bus and at-spi2-core")
    sys.exit(0)
try:
    import gi
    gi.require_version("Atspi", "2.0")
except (ImportError, ValueError):
    print("skipped: the Atspi GObject bindings aren't installed")
    sys.exit(0)

failures = []
bus = subprocess.Popen([launcher, "--launch-immediately"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.0)
env = dict(os.environ, GTK_MODULES="gail:atk-bridge", NO_AT_BRIDGE="0")
app = subprocess.Popen([sys.executable, os.path.join(REPO, "tests", "fixtures", "a11y_window.py")],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, text=True)
try:
    app.stdout.readline()
    time.sleep(1.5)
    elements = []
    for _ in range(10):
        elements = vision.accessibility_elements({"pid": app.pid})
        if any(e["text"] == "Submit" for e in elements):
            break
        time.sleep(0.5)
    names = {e["text"]: e for e in elements}
    for name in ("Submit", "Cancel"):
        if name not in names:
            failures.append(f"the {name} button wasn't found through accessibility: {sorted(names)}")
    if "Submit" in names:
        el = names["Submit"]
        if el["role"] != "push button":
            failures.append(f"it isn't reported as a button: {el['role']}")
        x, y, w, h = el["box"]
        if not (100 <= x <= 700 and 80 <= y <= 380 and w > 20 and h > 10):
            failures.append(f"its box isn't where the window put it: {el['box']}")
        if "Cancel" in names and names["Cancel"]["box"][0] <= x:
            failures.append("Cancel should be to the right of Submit")
    missing = vision.accessibility_elements({"pid": 999999})
    if missing:
        failures.append("an app that isn't running has no controls")
finally:
    app.terminate()
    bus.terminate()

if failures:
    print(f"{len(failures)} PROBLEM(S):")
    for f in failures:
        print("  ", f)
    sys.exit(1)
print("accessibility checks passed")
