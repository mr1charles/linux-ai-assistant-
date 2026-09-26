#!/usr/bin/env python3
"""
toby — the one command for Little Toby.

    toby                    summon Toby (same as the keybind)
    toby start | stop | restart | status
    toby sleep              play the lid fold in full, then suspend
    toby fold preview       play the fold and unfold, without suspending
    toby phone on | off | pair | devices | revoke <name> | reset | status
    toby animations on | off | status
    toby island on | off    show or hide the Dynamic Island pill
    toby telegram setup | pair | status | unpair <name> | off
    toby notes set <folder> | off | status | search <words>
    toby queue [add "<task>" | run | file]   things for Toby to do overnight
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
TELEGRAM_UNIT = Path.home() / ".config" / "systemd" / "user" / "toby-telegram.service"


def services():
    """Toby's services, with Telegram's once it's been set up."""
    return SERVICES + (("toby-telegram.service",) if TELEGRAM_UNIT.exists() else ())


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
    out = run(["systemctl", "--user", "start", *services()])
    if out.returncode:
        say("Couldn't start Toby's services: " + (out.stderr.strip() or "unknown error"))
        say("If you haven't installed yet, run ./install.sh from the project folder.")
        return 1
    say("Toby is running. Press your keybind, or run: toby")
    return 0


def cmd_stop(_args):
    run(["systemctl", "--user", "stop", *services()])
    say("Toby has stopped. Your notes and settings are untouched.")
    return 0


def cmd_restart(_args):
    run(["systemctl", "--user", "restart", *services()])
    say("Toby restarted.")
    return 0


def cmd_status(_args):
    for service in services():
        state = run(["systemctl", "--user", "is-active", service]).stdout.strip() or "unknown"
        say(f"{service:<20} {state}")
    settings = load_settings()
    import model_picker
    chat_url = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/chat")
    installed = model_picker.installed_models(chat_url)
    model = model_picker.pick(settings.get("ollama_model", ""), os.environ.get("OLLAMA_MODEL", ""), installed)
    say(f"{'model':<20} {model}" + ("" if installed else "  (Ollama not reachable)"))
    say(f"{'phone app':<20} {'on' if settings.get('remote_enabled') else 'off'}")
    say(f"{'telegram':<20} {'on' if settings.get('telegram_enabled') else 'off'}")
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
    import time as _time
    status, session = 0, {}
    for _attempt in range(15):          # Toby may still be starting up
        status, session = remote_bridge.admin_call("/api/admin/pair/start", {"base_url": base}, port=port)
        if status == 200:
            break
        _time.sleep(1)
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

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _service_active(name):
    return run(["systemctl", "--user", "is-active", name]).stdout.strip() == "active"


def _install_telegram_unit():
    TELEGRAM_UNIT.parent.mkdir(parents=True, exist_ok=True)
    text = (REPO / "systemd" / "toby-telegram.service").read_text().replace("@REPO@", str(REPO))
    TELEGRAM_UNIT.write_text(text)
    run(["systemctl", "--user", "daemon-reload"])


def cmd_telegram(args, ask=input, secret=None):
    import getpass
    import remote_bridge
    import telegram_bot as tb

    secret = secret or getpass.getpass
    action = args[0] if args else "status"
    config = tb.load_config()

    if action == "setup":
        say("Toby on Telegram: talk to Toby from any phone with Telegram, no app to install.")
        say("Good to know: Telegram bot chats aren't end-to-end encrypted; Telegram's servers carry")
        say("them. For a fully private connection use the phone app over Tailscale (toby phone on).")
        say()
        token = config.get("bot_token", "")
        if token and config.get("bot_username"):
            keep = ask(f"Keep using your bot @{config['bot_username']}? [Y/n] ").strip().lower()
            if keep in ("n", "no"):
                token = ""
        if not token:
            say("1. In Telegram, open @BotFather, send /newbot, and choose a name.")
            say("2. BotFather replies with a token like 123456789:AAF... Paste it here.")
            say("   (It isn't shown as you type. Anyone with it can pretend to be your bot, so keep it private.)")
            token = secret("Bot token: ").strip()
        api = tb.TelegramAPI(token, config.get("api_base", tb.API_BASE))
        try:
            me = api.call("getMe")
        except tb.TelegramError as e:
            say(f"Telegram didn't accept that token ({e.description}). Nothing was changed.")
            return 1
        store = remote_bridge.DeviceStore()
        if config.get("device_id"):
            store.revoke(config["device_id"])
        device, device_token = store.add(f"Telegram (@{me.get('username', 'bot')})", "telegram", tb.new_device_id())
        config.update(bot_token=token, bot_username=me.get("username", ""), device_token=device_token,
                      device_id=device["id"])
        config.setdefault("allowed_users", [])
        tb.save_config(config)
        save_setting(telegram_enabled=True)
        run(["systemctl", "--user", "stop", "toby-telegram.service"])
        if _service_active("toby.service"):
            run(["systemctl", "--user", "restart", "toby.service"])     # starts Toby's side of it
        say(f"Your bot is @{config['bot_username']}.")
        if not config["allowed_users"] or ask("Pair another Telegram account? [y/N] ").strip().lower() in ("y", "yes"):
            say()
            tb.pair_account(api, config, ask=ask, say=say)
            config = tb.load_config()
        _install_telegram_unit()
        out = run(["systemctl", "--user", "enable", "--now", "toby-telegram.service"])
        if out.returncode:
            say("Couldn't start the Telegram service: " + (out.stderr.strip() or "unknown error"))
            return 1
        who = ", ".join(u.get("name", str(u["id"])) for u in config.get("allowed_users", [])) or "nobody yet"
        say(f"Toby is listening on Telegram as @{config['bot_username']}, for: {who}.")
        return 0

    if not config.get("bot_token"):
        say("Telegram isn't set up. Run: toby telegram setup")
        return 1

    if action == "pair":
        was_running = _service_active("toby-telegram.service")
        run(["systemctl", "--user", "stop", "toby-telegram.service"])      # only one reader of the bot's messages
        try:
            tb.pair_account(tb.TelegramAPI(config["bot_token"], config.get("api_base", tb.API_BASE)),
                            config, ask=ask, say=say)
        finally:
            if was_running or TELEGRAM_UNIT.exists():
                run(["systemctl", "--user", "start", "toby-telegram.service"])
        return 0

    if action == "unpair":
        name = " ".join(args[1:]).strip().lower()
        users = config.get("allowed_users", [])
        keep = [u for u in users if name not in (str(u["id"]), u.get("name", "").lower())]
        if not name or len(keep) == len(users):
            say("Name one account exactly as `toby telegram status` lists it (or its id).")
            return 1
        config["allowed_users"] = keep
        tb.save_config(config)
        say("Unpaired. That Telegram account can't reach Toby any more.")
        return 0

    if action == "off":
        run(["systemctl", "--user", "disable", "--now", "toby-telegram.service"])
        if config.get("device_id"):
            remote_bridge.DeviceStore().revoke(config["device_id"])
        config.pop("device_token", None)
        config.pop("device_id", None)
        tb.save_config(config)
        save_setting(telegram_enabled=False)
        if _service_active("toby.service"):
            run(["systemctl", "--user", "restart", "toby.service"])
        say("Telegram is off: the bot can't reach Toby. `toby telegram setup` turns it back on.")
        return 0

    enabled = load_settings().get("telegram_enabled") and config.get("device_token")
    say(f"bot                 @{config.get('bot_username', '?')}")
    say(f"telegram            {'on' if enabled else 'off'}")
    say(f"service             {run(['systemctl', '--user', 'is-active', 'toby-telegram.service']).stdout.strip() or 'not installed'}")
    users = config.get("allowed_users", [])
    say("accounts            " + (", ".join(f"{u.get('name', '?')} ({u['id']})" for u in users) or "none paired"))
    return 0


# ---------------------------------------------------------------------------
# notes
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# the overnight queue
# ---------------------------------------------------------------------------

QUEUE_MARKS = {" ": "to do", "x": "done", "!": "needs you", "~": "working"}


def cmd_queue(args):
    import notes
    import task_queue

    settings = load_settings()
    path = task_queue.queue_path(notes.folder(settings))
    action = args[0] if args else "list"
    if action == "add":
        try:
            text = task_queue.add(path, " ".join(args[1:]))
        except ValueError:
            say('Say what to do, e.g.: toby queue add "summarize ~/project/README.md"')
            return 1
        say(f"Added: {text}")
        at = task_queue.parse(f"- [ ] {text}")[0].at
        when = (f"at {at[0]:02d}:{at[1]:02d}" if at else
                f"between {settings.get('queue_start', '01:00')} and {settings.get('queue_end', '07:00')}")
        say(f"Toby does it {when} if the computer is awake, or now with: toby queue run")
        return 0
    if action == "run":
        if not signal_process("linux_agent_apple.py", 0):
            say("Toby isn't running. Start it with: toby start")
            return 1
        task_queue.QueueRunner(lambda: path, None, None, lambda: settings, None).request_run_now()
        say("Toby will start on the queue within half a minute, once it isn't busy.")
        return 0
    if action == "file":
        say(str(task_queue.ensure(path)))
        return 0
    if not path.exists():
        say(f"The queue is empty. Add a task with: toby queue add \"…\"  (it lives in {path})")
        return 0
    items = task_queue.parse(path.read_text())
    if not items:
        say(f"The queue is empty. Add a task with: toby queue add \"…\"  (it lives in {path})")
    for item in items:
        say(f"{QUEUE_MARKS.get(item.state, item.state):<10} {item.text}")
    say(f"({path})")
    return 0


def _count(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def cmd_notes(args):
    import knowledge
    import notes

    action = args[0] if args else "status"
    settings = load_settings()
    if action == "set":
        raw = " ".join(args[1:]).strip()
        if not raw or notes.folder({"notes_folder": raw}) is None:
            say("Name a folder that exists, e.g.: toby notes set ~/Obsidian")
            return 1
        root = notes.folder({"notes_folder": raw})
        save_setting(notes_folder=raw)
        knowledge.use_notes(lambda: root)
        say(f"Reading your notes in {root}…")
        index = notes.NotesIndex(root)
        index.refresh(force=True)
        stats = index.stats()
        say(f"Toby can search {_count(stats['files'], 'note')} ({_count(stats['passages'], 'passage')})."
            + (f" {stats['too_big']} were too big to read." if stats["too_big"] else ""))
        say(f"Toby's memory is now {root / notes.TOBY_DIR / notes.MEMORY_FILE}; edit it freely.")
        say("Private-looking folders are skipped; list more in a .tobyignore file there.")
        if _service_active("toby.service"):
            run(["systemctl", "--user", "restart", "toby.service"])
            say("Toby restarted to use them.")
        return 0
    if action == "off":
        save_setting(notes_folder="")
        if _service_active("toby.service"):
            run(["systemctl", "--user", "restart", "toby.service"])
        say("Toby no longer reads your notes, and remembers things in ~/linux-agent/knowledge.json again.")
        say("Nothing in your notes folder was changed or deleted.")
        return 0
    root = notes.folder(settings)
    if root is None:
        say("No notes folder is set. Set one with: toby notes set ~/Obsidian")
        return 0 if action == "status" else 1
    index = notes.NotesIndex(root)
    if action == "search":
        query = " ".join(args[1:])
        hits = index.search(query, limit=8)
        if not hits:
            say("Nothing matched.")
        for h in hits:
            say(f"{h['rel']}" + (f" › {h['heading']}" if h["heading"] else ""))
            say(f"    {h['snippet']}")
        return 0
    index.refresh(force=True)
    stats = index.stats()
    say(f"notes folder        {root}")
    say(f"searchable          {_count(stats['files'], 'note')}, {_count(stats['passages'], 'passage')}")
    say(f"memory              {root / notes.TOBY_DIR / notes.MEMORY_FILE}")
    say(f"daily log           {'on' if settings.get('notes_daily_log', True) else 'off'}")
    return 0


def cmd_island(args):
    action = args[0] if args else "status"
    if action not in ("on", "off"):
        on = load_settings().get("island_enabled", True)
        say("The Dynamic Island is " + ("on." if on else "off.") + " Change it with: toby island on|off")
        return 0
    save_setting(island_enabled=(action == "on"))
    running = run(["systemctl", "--user", "is-active", "toby.service"]).stdout.strip() == "active"
    if running:
        run(["systemctl", "--user", "restart", "toby.service"])
    if action == "off":
        say("The Dynamic Island is off. It only appears now to tell you a phone is viewing your screen.")
    else:
        say("The Dynamic Island is back on.")
    if running:
        say("Toby restarted to apply it.")
    return 0


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
        changed = run(["git", "-C", str(REPO), "status", "--porcelain", "--untracked-files=no"], timeout=30)
        if changed.stdout.strip():
            # edits to Toby's own files would block the update; keep them, don't lose them
            run(["git", "-C", str(REPO), "stash", "push", "-q", "-m", "local changes, saved by toby update"], timeout=30)
            say(f"You had changed some of Toby's files. They're saved: git -C {REPO} stash list")
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
    "animations": cmd_animations, "island": cmd_island, "telegram": cmd_telegram, "notes": cmd_notes,
    "queue": cmd_queue,
    "model": cmd_model, "doctor": cmd_doctor,
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
