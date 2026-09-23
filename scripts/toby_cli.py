#!/usr/bin/env python3
"""
toby — the one command for Little Toby.

    toby                    summon Toby (same as the keybind)
    toby start | stop | restart | status
    toby sleep              play the lid fold in full, then suspend
    toby fold preview       play the fold and unfold, without suspending
    toby phone on | off | pair | devices | revoke <name> | reset | status
    toby animations on | off | status
    toby model [name]       show the model in use, or choose one
    toby doctor             check everything, change nothing
    toby update             pull the latest version and refresh dependencies
    toby uninstall          remove the services and keybind (keeps your data)

Every command explains what it did in a sentence, and none of them edits
your Hyprland or system configuration without saying so first.
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

SERVICES = ("toby.service", "toby-fold.service")


def say(text=""):
    print(text, flush=True)


def run(cmd, timeout=10, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


def have(binary):
    return shutil.which(binary) is not None


def signal_process(pattern, sig):
    """Signal a running Toby process. Returns True if one was found."""
    out = run(["pgrep", "-f", pattern])
    pids = [int(p) for p in out.stdout.split() if p.isdigit() and int(p) != os.getpid()]
    for pid in pids:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
    return bool(pids)


def load_settings():
    import toby_settings
    return toby_settings.load()


def save_setting(**changes):
    import toby_settings
    settings = toby_settings.load()
    settings.update(changes)
    toby_settings.save(settings)
    return settings


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------

def cmd_show(_args):
    if not signal_process("linux_agent_apple.py", signal.SIGUSR1):
        say("Toby isn't running. Start it with: toby start")
        return 1
    return 0


def cmd_start(_args):
    out = run(["systemctl", "--user", "start", *SERVICES])
    if out.returncode:
        say("Couldn't start Toby's services: " + (out.stderr.strip() or "unknown error"))
        say("If you haven't installed yet, run ./install.sh from the project folder.")
        return 1
    say("Toby is running. Press your keybind, or run: toby")
    return 0


def cmd_stop(_args):
    run(["systemctl", "--user", "stop", *SERVICES])
    say("Toby has stopped. Your notes and settings are untouched.")
    return 0


def cmd_restart(_args):
    run(["systemctl", "--user", "restart", *SERVICES])
    say("Toby restarted.")
    return 0


def cmd_status(_args):
    for service in SERVICES:
        state = run(["systemctl", "--user", "is-active", service]).stdout.strip() or "unknown"
        say(f"{service:<20} {state}")
    settings = load_settings()
    import model_picker
    chat_url = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/chat")
    installed = model_picker.installed_models(chat_url)
    model = model_picker.pick(settings.get("ollama_model", ""), os.environ.get("OLLAMA_MODEL", ""), installed)
    say(f"{'model':<20} {model}" + ("" if installed else "  (Ollama not reachable)"))
    say(f"{'phone app':<20} {'on' if settings.get('remote_enabled') else 'off'}")
    return 0


# ---------------------------------------------------------------------------
# the fold
# ---------------------------------------------------------------------------

def cmd_sleep(_args):
    if signal_process("toby_fold.py", signal.SIGUSR1):
        return 0
    say("The fold animation isn't running, so suspending without it.")
    run(["systemctl", "suspend"])
    return 0


def cmd_fold(args):
    if args[:1] == ["preview"]:
        if signal_process("toby_fold.py", signal.SIGUSR2):
            return 0
        say("The fold animation isn't running. Start it with: toby start")
        return 1
    say("Usage: toby fold preview")
    return 1


# ---------------------------------------------------------------------------
# phone
# ---------------------------------------------------------------------------

def tailscale_name():
    """This computer's name on your tailnet, if Tailscale is up."""
    import remote_bridge
    return remote_bridge.tailscale_name()


def lan_address():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))   # never sent; only picks the outgoing interface
        addr = s.getsockname()[0]
        s.close()
        return addr
    except OSError:
        return None


def show_qr(text):
    if have("qrencode"):
        out = run(["qrencode", "-t", "ansiutf8", "-m", "2", text])
        if out.returncode == 0:
            print(out.stdout)
            return True
    return False


def phone_base_url(settings, port):
    """The address a phone uses to reach this computer, or (None, why not)."""
    name = tailscale_name()
    if name:
        return f"https://{name}", "from anywhere your phone has internet (via Tailscale)"
    if settings.get("remote_bind", "127.0.0.1") == "127.0.0.1":
        return None, ("Without Tailscale, Toby only listens on this computer. Install Tailscale on "
                      "this computer and your phone and run `sudo tailscale up` (recommended), or set "
                      "\"remote_bind\": \"0.0.0.0\" in settings.json to allow phones on this Wi-Fi only "
                      "(traffic on your Wi-Fi is then not encrypted).")
    return f"http://{lan_address() or 'this-computer'}:{port}", "from this Wi-Fi only"


def pair_phone(settings, port, ask=input):
    """Pair a phone from the terminal, through the running Toby."""
    import remote_bridge
    base, where = phone_base_url(settings, port)
    if base is None:
        say(where)
        return 1
    status, session = remote_bridge.admin_call("/api/admin/pair/start", {"base_url": base}, port=port)
    if status != 200:
        say("Couldn't reach the running Toby to start pairing. Is it running? Try: toby start")
        return 1
    say("On your iPhone, open Little Toby and tap Pair a computer, then scan this code.")
    say("(Scanned with the ordinary camera, it opens the web version instead.)")
    if not show_qr(session["url"]):
        say("(install qrencode to see a QR code here)")
    say(f"Or type the address and code by hand:  {base}   {session['display_code']}")
    say(f"It will work {where}. The code expires in {session['expires_in'] // 60} minutes.")
    say("")
    say("Waiting for your phone…")
    deadline = session["expires_in"]
    waited = 0
    while waited < deadline:
        status, body = remote_bridge.admin_call(
            f"/api/admin/pair/claims?session={session['id']}&wait=25", port=port, timeout=35)
        waited += 25
        claims = body.get("claims", []) if status == 200 else []
        if not claims:
            continue
        claim = claims[0]
        say(f"{claim['name']} ({claim['platform']}) wants to pair.")
        say(f"Your phone should show the number {claim['compare'][:3]} {claim['compare'][3:]}.")
        answer = ask("If it does, pair it? [y/N] ").strip().lower()
        allow = answer in ("y", "yes")
        remote_bridge.admin_call("/api/admin/pair/decide", {"claim": claim["id"], "allow": allow}, port=port)
        say("Paired. It can now reach Toby." if allow else "Not paired.")
        return 0 if allow else 1
    say("The code expired before a phone used it. Run `toby phone pair` to try again.")
    return 1


def cmd_phone(args):
    import remote_bridge

    action = args[0] if args else "status"
    settings = load_settings()
    port = int(settings.get("remote_port", 8765))

    if action == "on":
        save_setting(remote_enabled=True)
        name = tailscale_name()
        if name:
            out = run(["tailscale", "serve", "--bg", f"http://127.0.0.1:{port}"], timeout=20)
            if out.returncode:
                say("Tailscale couldn't publish Toby: " + out.stderr.strip())
                say("You may need to allow HTTPS for your tailnet once, at "
                    "https://login.tailscale.com/admin/dns (turn on HTTPS Certificates).")
        else:
            say("Tailscale isn't set up, so phones will only reach Toby on this Wi-Fi, if at all.")
            say("For anywhere-access, install Tailscale on the computer and phone and run: sudo tailscale up")
        run(["systemctl", "--user", "restart", "toby.service"])
        say("The phone connection is on.")
        import time as _time
        _time.sleep(2)   # give Toby a moment to come back up
        return cmd_phone(["pair"])

    if action == "off":
        save_setting(remote_enabled=False)
        if have("tailscale"):
            run(["tailscale", "serve", "reset"], timeout=20)
        run(["systemctl", "--user", "restart", "toby.service"])
        say("The phone connection is off. Toby is no longer reachable from your phone.")
        return 0

    if action == "reset":
        remote_bridge.DeviceStore().reset()
        say("Every phone has been unpaired. Run `toby phone pair` to pair again.")
        return 0

    if action == "pair":
        if not settings.get("remote_enabled"):
            say("The phone connection is off. Turn it on with: toby phone on")
            return 1
        return pair_phone(settings, port)

    if action == "devices":
        devices = remote_bridge.DeviceStore().devices()
        if not devices:
            say("No phones are paired.")
        for d in devices:
            import time as _time
            seen = _time.strftime("%d %b %H:%M", _time.localtime(d["last_seen"])) if d.get("last_seen") else "never"
            say(f"{d['name']:<28} {d['platform']:<6} last used {seen}")
        return 0

    if action == "revoke":
        name = " ".join(args[1:]).strip().lower()
        store = remote_bridge.DeviceStore()
        matches = [d for d in store.devices() if d["name"].lower() == name or d["id"] == name]
        if len(matches) != 1:
            say("Name one paired phone exactly, as `toby phone devices` lists it.")
            return 1
        store.revoke(matches[0]["id"])
        say(f"{matches[0]['name']} is unpaired.")
        return 0

    say(f"phone connection: {'on' if settings.get('remote_enabled') else 'off'}")
    say(f"tailscale: {tailscale_name() or 'not set up'}")
    say(f"paired phones: {len(remote_bridge.DeviceStore().devices())}")
    return 0


# ---------------------------------------------------------------------------
# animations
# ---------------------------------------------------------------------------

def cmd_animations(args):
    import hypr_animations
    import toby_anim

    action = args[0] if args else "status"
    if action == "on":
        speed = toby_anim.animation_settings(load_settings())["hypr_animation_speed"]
        save_setting(animations={**load_settings().get("animations", {}), "hypr_animations_enabled": True})
        applied, failed = hypr_animations.apply(speed)
        active = hypr_animations.active_toby_animations()
        if active:
            say(f"Applied Toby's motion to {len(active)} kinds of window and workspace animation.")
        elif applied:
            say("Hyprland accepted the animations but didn't report them back — they may still be active.")
        else:
            say("Hyprland didn't accept the animations. Nothing was changed.")
            return 1
        say("Nothing was written to your config; `toby animations off` or a Hyprland reload undoes it.")
        return 0
    if action == "off":
        save_setting(animations={**load_settings().get("animations", {}), "hypr_animations_enabled": False})
        ok = hypr_animations.restore()
        say("Reloaded your Hyprland config; your own animations are back." if ok
            else "Couldn't reach Hyprland to reload it.")
        return 0 if ok else 1
    active = hypr_animations.active_toby_animations()
    say(f"Toby's window animations: {'active' if active else 'not active'}")
    return 0


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------

def cmd_model(args):
    import model_picker

    chat_url = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/chat")
    if args:
        name = args[0]
        if name == "auto":
            save_setting(ollama_model="")
            say("Toby will use the best model you have installed.")
        else:
            if name not in model_picker.installed_models(chat_url):
                say(f"Downloading {name} first…")
                if subprocess.call(["ollama", "pull", name]) != 0:
                    say("Couldn't download it; nothing changed.")
                    return 1
            save_setting(ollama_model=name)
            say(f"Toby will use {name}.")
        run(["systemctl", "--user", "restart", "toby.service"])
        return 0
    settings = load_settings()
    installed = model_picker.installed_models(chat_url)
    chosen = model_picker.pick(settings.get("ollama_model", ""), os.environ.get("OLLAMA_MODEL", ""), installed)
    say(f"In use: {chosen}")
    if installed:
        say("Installed: " + ", ".join(installed))
    say("Choose one with `toby model <name>`, or `toby model auto`.")
    return 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def cmd_doctor(_args):
    """Look at everything Toby depends on and report. Changes nothing."""
    import toby_doctor
    problems = toby_doctor.run_all(say)
    return 1 if problems else 0


# ---------------------------------------------------------------------------
# update / uninstall
# ---------------------------------------------------------------------------

def cmd_update(_args):
    if (REPO / ".git").exists():
        out = run(["git", "-C", str(REPO), "pull", "--ff-only"], timeout=120)
        say(out.stdout.strip() or out.stderr.strip())
        if out.returncode:
            say("Couldn't update automatically (local changes?). Nothing was changed.")
            return 1
    return subprocess.call([str(REPO / "install.sh"), "--update"])


def cmd_uninstall(_args):
    return subprocess.call([str(REPO / "uninstall.sh")])


COMMANDS = {
    "show": cmd_show, "start": cmd_start, "stop": cmd_stop, "restart": cmd_restart,
    "status": cmd_status, "sleep": cmd_sleep, "fold": cmd_fold, "phone": cmd_phone,
    "animations": cmd_animations, "model": cmd_model, "doctor": cmd_doctor,
    "update": cmd_update, "uninstall": cmd_uninstall,
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return cmd_show([])
    if argv[0] in ("-h", "--help", "help"):
        say(__doc__.strip().split("\n\n")[1])
        return 0
    command = COMMANDS.get(argv[0])
    if command is None:
        say(f"Unknown command: {argv[0]}. Try: toby help")
        return 2
    return command(argv[1:]) or 0


if __name__ == "__main__":
    sys.exit(main())
