"""Checks for screen_control.py's ydotool handling.

ydotool 1.0 changed the argument syntax for both `key` and `click`, and the
old spellings fail quietly: they print usage text and exit non-zero, which
under check=False is indistinguishable from success. That made it possible
for Toby to report "Clicked left" for a click that never happened. These
checks run against a stand-in ydotool that records its arguments and can be
told to reject them, so the fallback path is exercised for real.
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

# screen_control imports GLib for the mouse glide timer; stub it so this
# check needs nothing but the standard library.
import types  # noqa: E402
gi = types.ModuleType("gi")
repository = types.ModuleType("gi.repository")
pending_timers = []
repository.GLib = types.SimpleNamespace(
    timeout_add=lambda _ms, fn, *a: pending_timers.append(fn) or len(pending_timers))
gi.repository = repository
sys.modules.setdefault("gi", gi)
sys.modules.setdefault("gi.repository", repository)

import screen_control as sc  # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


workdir = tempfile.mkdtemp()
log_path = os.path.join(workdir, "calls.log")
stub_path = os.path.join(workdir, "ydotool")


def install_stub(reject_modern_syntax):
    """Write a fake ydotool. When reject_modern_syntax is set it behaves like
    a pre-1.0 build: it rejects keycode and hex-button arguments."""
    with open(stub_path, "w") as f:
        f.write(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"reject = {bool(reject_modern_syntax)}\n"
            f"open({log_path!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
            "args = sys.argv[1:]\n"
            "if reject and args and args[0] == 'key' and ':' in ' '.join(args[1:]):\n"
            "    sys.exit(1)\n"
            "if reject and args and args[0] == 'click' and args[1].startswith('0x'):\n"
            "    sys.exit(1)\n"
            "if not reject and args and args[0] == 'key' and ':' not in ' '.join(args[1:]):\n"
            "    sys.exit(1)\n"
            "if not reject and args and args[0] == 'click' and not args[1].startswith('0x'):\n"
            "    sys.exit(1)\n"
            "sys.exit(0)\n"
        )
    os.chmod(stub_path, 0o755)


def calls():
    try:
        with open(log_path) as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        return []


def reset_log():
    if os.path.exists(log_path):
        os.remove(log_path)


original_path = os.environ.get("PATH", "")
os.environ["PATH"] = workdir + os.pathsep + original_path

# -- the combo translation itself -------------------------------------------
check("ctrl+w translates to keycodes", sc.keycode_sequence("ctrl+w"),
      ["29:1", "17:1", "17:0", "29:0"])
check("modifiers release in reverse order", sc.keycode_sequence("ctrl+shift+t"),
      ["29:1", "42:1", "20:1", "20:0", "42:0", "29:0"])
check("super is the meta key", sc.keycode_sequence("super+q"),
      ["125:1", "16:1", "16:0", "125:0"])
check("spacing and case are tolerated", sc.keycode_sequence(" CTRL + C "),
      sc.keycode_sequence("ctrl+c"))
check("an unknown key gives up rather than guessing",
      sc.keycode_sequence("ctrl+nonsense"), None)
check("an empty combo gives up", sc.keycode_sequence(""), None)

# -- against a current ydotool ----------------------------------------------
install_stub(reject_modern_syntax=False)
reset_log()
check("key press succeeds on ydotool 1.0", sc.press_keys("ctrl+w"), "Pressed: ctrl+w")
check("it used the keycode syntax", calls(), ["key 29:1 17:1 17:0 29:0"])

reset_log()
check("click succeeds on ydotool 1.0", sc.press_button("left"), "Clicked left")
check("it used the hex button code", calls(), ["click 0xC0"])

reset_log()
check("an unknown button falls back to left", sc.press_button("sideways"), "Clicked left")
check("the fallback still used a left click", calls(), ["click 0xC0"])

# -- against an older ydotool that wants the name form ----------------------
install_stub(reject_modern_syntax=True)
reset_log()
check("key press still succeeds on an older ydotool",
      sc.press_keys("ctrl+w"), "Pressed: ctrl+w")
check("it retried with the name syntax",
      calls(), ["key 29:1 17:1 17:0 29:0", "key ctrl+w"])

reset_log()
check("click still succeeds on an older ydotool", sc.press_button("right"), "Clicked right")
check("it retried with the numeric button", calls(), ["click 0xC1", "click 1"])

# -- with no ydotool installed at all ---------------------------------------
os.environ["PATH"] = original_path
os.remove(stub_path)
check("a missing ydotool is reported, not silently ignored",
      "ydotool isn't installed" in sc.press_keys("ctrl+w"), True)
check("a missing ydotool is reported for clicks too",
      "ydotool isn't installed" in sc.press_button("left"), True)

# -- the consent gate still holds -------------------------------------------
control = sc.ScreenControl()
for name, call in [("click", lambda: control.click()),
                   ("type_text", lambda: control.type_text("x")),
                   ("key", lambda: control.key("ctrl+c")),
                   ("move", lambda: control.move(0.5, 0.5))]:
    try:
        call()
        failures.append(f"{name} ran without consent being granted")
    except sc.NeedsConfirmation:
        pass

control.grant()
check("granting consent opens the gate", control.enabled, True)

# -- a glide, run the way the app runs it -------------------------------------
# The task runner calls move() on a worker thread; the glide's frames run on
# the main loop (here, this thread pumping the timers by hand).
import threading  # noqa: E402
import time  # noqa: E402

pointer_log = []
sc.move_pointer = lambda px, py, timeout=1.5: pointer_log.append((px, py)) or "ok"
sc.read_cursor = lambda timeout=0.5: (100.0, 100.0)   # the user left it here
sc.press_button = lambda button, timeout=2: pointer_log.append(("click", button)) or f"Clicked {button}"
control.get_screen_size = lambda: (1000, 800)
control.current_x_frac, control.current_y_frac = 0.9, 0.9   # a stale memory
pending_timers.clear()
plan_seen = []


def pump_glide(until):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not until():
        for fn in list(pending_timers):
            if not fn():
                pending_timers.remove(fn)
        pos = control.pointer_pixels()
        if pos is not None:
            plan_seen.append(pos)
        time.sleep(0.01)


results = {}
worker = threading.Thread(target=lambda: results.setdefault("move", control.move(0.5, 0.25)))
worker.start()
pump_glide(lambda: not worker.is_alive())
worker.join(1)
check("move() on the task thread waits until the pointer arrives",
      results.get("move"), "Moved the mouse to (500, 200)")
check("the glide started from where the pointer really was, not a stale memory",
      pointer_log[0][0] < 200 and pointer_log[0][1] < 200, True)
check("and it ends exactly on the target", pointer_log[-1], (500, 200))
check("the tracked position is the target afterwards",
      (round(control.current_x_frac, 3), round(control.current_y_frac, 3)), (0.5, 0.25))
steps = [((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5 for a, b in zip(pointer_log, pointer_log[1:])]
check("the pointer never jumps across the screen", max(steps) < 250, True)

# a click that follows a move can't land while the pointer is on its way
pointer_log.clear()
control.carry_speed = 900.0   # the chibi is carrying it: a slower, walking-pace glide
order = []
worker = threading.Thread(target=lambda: (control.move(0.9, 0.9), order.append("moved")))
clicker = threading.Thread(target=lambda: (time.sleep(0.05), order.append(control.click())))
worker.start()
clicker.start()
pump_glide(lambda: not worker.is_alive() and not clicker.is_alive())
check("the click happened", "Clicked left" in order, True)
check("it waited for the glide, so it clicked at the destination",
      pointer_log[-2:], [(900, 720), ("click", "left")])
check("and no pointer move came after the click", pointer_log[-1], ("click", "left"))
check("carrying glides take walking time, not the quick glide",
      control.glide_seconds(900) > 0.9, True)
control.carry_speed = None
check("without the chibi, a glide stays quick", control.glide_seconds(900), 0.34)
check("pointer_pixels reports the resting position when nothing moves",
      control.pointer_pixels(), (900.0, 720.0))
check("disable closes it again", control.disable(), "Screen control disabled.")
check("and it is really closed", control.enabled, False)

if failures:
    print(f"{len(failures)} PROBLEM(S):")
    for f in failures:
        print("  ", f)
    sys.exit(1)
print("screen control checks passed")
