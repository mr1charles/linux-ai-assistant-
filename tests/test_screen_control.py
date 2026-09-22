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
repository.GLib = types.SimpleNamespace(timeout_add=lambda *a, **k: None)
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
check("disable closes it again", control.disable(), "Screen control disabled.")
check("and it is really closed", control.enabled, False)

if failures:
    print(f"{len(failures)} PROBLEM(S):")
    for f in failures:
        print("  ", f)
    sys.exit(1)
print("screen control checks passed")
