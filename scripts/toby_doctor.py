"""
toby_doctor.py — `toby doctor`: check everything, change nothing.

Looks at each thing Toby depends on and says, in plain words, whether it's
fine and what to do if not. In particular it inspects exactly what the lid
fold has to cooperate with — how logind handles the lid, how long it lets
programs delay sleep, whether the fold daemon actually holds its delay
lock, and whether your Hyprland config does something of its own when the
lid closes — because those differ between machines and can't be assumed.

Nothing here writes a file, changes a setting or restarts anything.
"""

import importlib
import json
import os
import shutil
import subprocess

OK, WARN, BAD = "ok", "warn", "fail"


def _run(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


class Report:
    def __init__(self, say):
        self.say = say
        self.problems = 0

    def section(self, title):
        self.say("")
        self.say(title)

    def line(self, level, text, hint=None):
        mark = {"ok": "  ok  ", "warn": "  note", "fail": "  FIX "}[level]
        self.say(f"{mark}  {text}")
        if hint:
            self.say(f"        {hint}")
        if level == BAD:
            self.problems += 1


def check_python(r):
    r.section("Python packages")
    needed = [
        ("gi", "python-gobject", True), ("cairo", "python-cairo", True),
        ("requests", "python-requests", True),
        ("vosk", "pip: vosk (Voice Mode only)", False),
        ("sounddevice", "pip: sounddevice (Voice Mode only)", False),
        ("mediapipe", "pip: mediapipe (Camera Mode only)", False),
        ("cv2", "pip: opencv-python (Camera Mode only)", False),
    ]
    for module, package, required in needed:
        try:
            importlib.import_module(module)
            r.line(OK, module)
        except Exception:
            r.line(BAD if required else WARN, f"{module} is missing", f"install {package}")
    try:
        import gi
        gi.require_version("GtkLayerShell", "0.1")
        r.line(OK, "gtk-layer-shell")
    except Exception:
        r.line(BAD, "gtk-layer-shell is missing", "sudo pacman -S gtk-layer-shell")


def check_programs(r):
    r.section("Programs")
    for binary, why, required in [
        ("ollama", "the local AI model", True), ("hyprctl", "Hyprland", True),
        ("grim", "screenshots for the lid fold and screen reading", True),
        ("ydotool", "mouse and keyboard control", False),
        ("tesseract", "reading text on screen", False),
        ("espeak-ng", "Toby's voice", False),
        ("fprintd-verify", "fingerprint approval", False),
        ("tailscale", "reaching Toby from your phone anywhere", False),
        ("qrencode", "showing the phone pairing code", False),
    ]:
        if shutil.which(binary):
            r.line(OK, f"{binary} ({why})")
        else:
            r.line(BAD if required else WARN, f"{binary} not found — needed for {why}")


def check_services(r):
    r.section("Services")
    for service in ("toby.service", "toby-fold.service"):
        state = _run(["systemctl", "--user", "is-active", service]).stdout.strip()
        if state == "active":
            r.line(OK, f"{service} is running")
        else:
            r.line(WARN, f"{service} is {state or 'not installed'}", "start it with: toby start")
    ydotool = _run(["systemctl", "--user", "is-active", "ydotool.service"]).stdout.strip()
    ydotool_sys = _run(["systemctl", "is-active", "ydotool.service"]).stdout.strip()
    if "active" in (ydotool, ydotool_sys):
        r.line(OK, "ydotoold is running")
    else:
        r.line(WARN, "ydotoold isn't running, so mouse and keyboard control will time out",
               "systemctl --user enable --now ydotool.service")


def check_model(r):
    r.section("AI model")
    import model_picker
    url = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/chat")
    installed = model_picker.installed_models(url)
    if not installed:
        r.line(BAD, "Ollama isn't answering", "sudo systemctl enable --now ollama")
        return
    best = next((m for m in model_picker.PREFERRED if m in installed), None)
    r.line(OK, "installed models: " + ", ".join(installed))
    if best != model_picker.PREFERRED[0]:
        r.line(WARN, f"the fastest recommended model isn't installed",
               f"ollama pull {model_picker.PREFERRED[0]}")


def _logind_property(name):
    out = _run(["busctl", "get-property", "org.freedesktop.login1", "/org/freedesktop/login1",
                "org.freedesktop.login1.Manager", name])
    if out.returncode:
        return None
    parts = out.stdout.strip().split(" ", 1)
    return parts[1].strip('"') if len(parts) == 2 else None


def check_lid(r):
    r.section("Lid fold")
    lid = _logind_property("HandleLidSwitch")
    docked = _logind_property("HandleLidSwitchDocked")
    external = _logind_property("HandleLidSwitchExternalPower")
    if lid is None:
        r.line(WARN, "couldn't ask logind how the lid is handled")
    else:
        r.line(OK if lid in ("suspend", "hybrid-sleep", "suspend-then-hibernate") else WARN,
               f"closing the lid: {lid}" + (f" (on power: {external}, docked: {docked})" if external else ""),
               None if lid in ("suspend", "hybrid-sleep", "suspend-then-hibernate")
               else "the fold plays when the laptop suspends; with this setting the lid doesn't suspend it")

    delay = _logind_property("InhibitDelayMaxUSec")
    try:
        delay_s = int(delay) / 1_000_000 if delay else None
    except ValueError:
        delay_s = None
    if delay_s is not None:
        if delay_s < 1.0:
            r.line(WARN, f"logind lets programs delay sleep by only {delay_s:.1f}s — the fold may be cut short")
        else:
            r.line(OK, f"logind allows up to {delay_s:.0f}s of delay; the fold needs under 1s")

    held = _run(["systemd-inhibit", "--list", "--no-pager"]).stdout
    if "Little Toby" in held:
        r.line(OK, "the fold daemon holds its delay lock")
    else:
        r.line(WARN, "the fold daemon doesn't hold a delay lock right now", "start it with: toby start")

    binds = _run(["hyprctl", "-j", "binds"])
    if binds.returncode != 0:
        r.line(WARN, "couldn't read Hyprland's key bindings, so couldn't check for lid actions")
        r.line(OK, "preview it any time with: toby fold preview")
        return
    lid_actions = []
    try:
        for bind in json.loads(binds.stdout or "[]"):
            key = str(bind.get("key", ""))
            if "switch" in key.lower() or "lid" in key.lower():
                lid_actions.append(f"{bind.get('dispatcher', '')} {bind.get('arg', '')}".strip())
    except ValueError:
        pass
    if lid_actions:
        r.line(WARN, "your Hyprland config also acts on the lid: " + "; ".join(lid_actions),
               "if that turns the screen off first, the closing fold won't be seen — the "
               "unfold on opening still plays, and `toby sleep` shows the whole fold")
    else:
        r.line(OK, "no lid bindings in Hyprland competing with the fold")
    r.line(OK, "preview it any time with: toby fold preview")


def check_keybind(r):
    r.section("Summon key")
    import hypr_keybind
    import toby_settings
    combo = toby_settings.load().get("summon_keybind", "SUPER, G")
    if not combo:
        r.line(OK, "no summon key (turned off in settings); use the `toby` command")
        return
    current = hypr_keybind.existing_binding(combo)
    if current is None:
        r.line(WARN, f"{combo} isn't bound yet — Toby binds it when it starts")
    elif "linux_agent_apple" in current:
        r.line(OK, f"{combo} summons Toby")
    else:
        r.line(WARN, f"{combo} already does something else ({current}), so Toby left it alone",
               'pick another key: set "summon_keybind" in settings.json, e.g. "SUPER, T"')


def check_permissions(r):
    r.section("Access")
    groups = _run(["id", "-nG"]).stdout.split()
    if "input" in groups:
        r.line(OK, "you're in the input group (needed by ydotool)")
    else:
        r.line(WARN, "you're not in the input group, so ydotool can't create a virtual device",
               "sudo usermod -aG input $USER   (then log out and back in)")


def run_all(say):
    r = Report(say)
    say("Little Toby doctor — this only looks; it changes nothing.")
    for check in (check_python, check_programs, check_services, check_model, check_lid, check_keybind, check_permissions):
        try:
            check(r)
        except Exception as e:
            r.line(WARN, f"{check.__name__} couldn't finish: {e}")
    say("")
    say("Everything needed is in place." if not r.problems
        else f"{r.problems} thing(s) above need fixing before Toby works fully.")
    return r.problems
