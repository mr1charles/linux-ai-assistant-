"""
computer_tools.py — the things Toby can do on the computer besides clicking.

These are the tools behind remote requests like "is my game still running?",
"put this file in Downloads", "run the tests" or "open my project in a
terminal". Each returns a short plain-text result, because that's what goes
back to the model (and, through it, to you); the structured versions feed
the phone's status and overview screens.

Everything here reports what is actually true. A value that can't be read
(no battery, no Hyprland) is None, never a guess. Permission is not decided
here — the task runner classifies every action with permissions.py and asks
you first when it should — but the file tools still refuse to write outside
your home folder, as a second line of defence.

Nothing is deleted outright: "delete" means the freedesktop trash, and a file
Toby overwrites is copied to ~/.local/share/little-toby/backups first.
"""

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

import permissions

HOME = Path.home()
BACKUP_DIR = HOME / ".local" / "share" / "little-toby" / "backups"
SKIP_DIRS = {".git", "node_modules", ".cache", "__pycache__", ".venv", "venv", ".local", ".npm",
             ".cargo", ".rustup", "target", ".mozilla", ".steam", ".var", "snap", ".gradle"}
# Stopping any of these would take the desktop, the connection to your phone
# or Toby's own hands down with it.
PROTECTED_PROGRAMS = {"hyprland", "systemd", "dbus-daemon", "dbus-broker", "pipewire", "wireplumber",
                      "sddm", "gdm", "login", "sshd", "tailscaled", "xwayland", "ydotoold", "ollama",
                      "quickshell", "caelestia", "noctalia-shell", "waybar"}
TOBY_MARKERS = ("linux_agent_apple", "toby_fold", "toby_cli", "remote_bridge")
READ_LIMIT = 20000

_touched = []
_touched_lock = threading.Lock()


def touched(reset=False):
    """Files touched since the last reset, newest last: [(path, action)]."""
    with _touched_lock:
        items = list(_touched)
        if reset:
            _touched.clear()
    return items


def _touch(path, action):
    with _touched_lock:
        _touched.append((permissions.pretty(path), action))
        del _touched[:-100]


def _run(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

class _CpuSampler:
    """CPU use from /proc/stat, as the change between two readings."""

    def __init__(self):
        self._last = None
        self._lock = threading.Lock()

    @staticmethod
    def _read():
        try:
            with open("/proc/stat") as f:
                parts = [int(x) for x in f.readline().split()[1:]]
        except (OSError, ValueError):
            return None
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        return sum(parts), idle

    def percent(self):
        with self._lock:
            now = self._read()
            if now is None:
                return None
            last = self._last
            if last is None or now[0] - last[0] <= 0:
                time.sleep(0.25)
                last, now = now, self._read()
            self._last = now
            total, idle = now[0] - last[0], now[1] - last[1]
            if total <= 0:
                return None
            return round(100.0 * (total - idle) / total, 1)


_cpu = _CpuSampler()


def _os_name():
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return None


def _memory():
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            info[key] = int(value.split()[0]) * 1024
        total, available = info["MemTotal"], info.get("MemAvailable", info.get("MemFree", 0))
        return {"total": total, "used": total - available,
                "percent": round(100.0 * (total - available) / total, 1)}
    except (OSError, ValueError, KeyError, ZeroDivisionError):
        return None


def _battery():
    base = Path("/sys/class/power_supply")
    try:
        supplies = sorted(base.iterdir())
    except OSError:
        return None
    for supply in supplies:
        try:
            if (supply / "type").read_text().strip() != "Battery":
                continue
            capacity = int((supply / "capacity").read_text().strip())
            status = (supply / "status").read_text().strip()
            return {"percent": capacity, "status": status.lower(),
                    "charging": status in ("Charging", "Full")}
        except (OSError, ValueError):
            continue
    return None


def _uptime():
    try:
        return int(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def system_status():
    """Real measurements of this computer. Missing values are None."""
    disk = None
    try:
        usage = shutil.disk_usage(HOME)
        disk = {"total": usage.total, "used": usage.used, "percent": round(100.0 * usage.used / usage.total, 1)}
    except OSError:
        pass
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        load = None
    return {"hostname": socket.gethostname(), "os": _os_name(), "kernel": os.uname().release,
            "uptime_s": _uptime(), "cpu_percent": _cpu.percent(), "memory": _memory(),
            "battery": _battery(), "disk": disk, "load": load, "sampled_at": time.time()}


def describe_status(status=None):
    s = status or system_status()
    parts = [f"{s['hostname']}" + (f" ({s['os']})" if s.get("os") else "")]
    if s.get("cpu_percent") is not None:
        parts.append(f"CPU {s['cpu_percent']:.0f}%")
    if s.get("memory"):
        parts.append(f"memory {s['memory']['percent']:.0f}% used")
    if s.get("battery"):
        b = s["battery"]
        parts.append(f"battery {b['percent']}% ({b['status']})")
    if s.get("disk"):
        parts.append(f"disk {s['disk']['percent']:.0f}% full")
    if s.get("uptime_s") is not None:
        hours, rest = divmod(s["uptime_s"], 3600)
        parts.append(f"up {hours}h {rest // 60}m")
    return ", ".join(parts) + "."


# ---------------------------------------------------------------------------
# Windows (Hyprland)
# ---------------------------------------------------------------------------

def list_windows():
    """Open windows, as Hyprland reports them. [] if it can't be asked."""
    out = _run(["hyprctl", "-j", "clients"])
    active = _run(["hyprctl", "-j", "activewindow"])
    try:
        clients = json.loads(out.stdout or "[]")
    except ValueError:
        return []
    try:
        active_address = json.loads(active.stdout or "{}").get("address")
    except ValueError:
        active_address = None
    windows = []
    for c in clients if isinstance(clients, list) else []:
        if not c.get("mapped", True) or c.get("hidden"):
            continue
        at, size = c.get("at") or [0, 0], c.get("size") or [0, 0]
        windows.append({"address": c.get("address", ""), "title": c.get("title", "")[:120],
                        "app": c.get("class") or c.get("initialClass") or "",
                        "workspace": (c.get("workspace") or {}).get("name", ""),
                        "pid": c.get("pid"), "focused": c.get("address") == active_address,
                        "x": at[0], "y": at[1], "w": size[0], "h": size[1],
                        "fullscreen": bool(c.get("fullscreen"))})
    windows.sort(key=lambda w: (not w["focused"], str(w["workspace"]), w["app"].lower()))
    return windows


def describe_windows(windows=None):
    windows = list_windows() if windows is None else windows
    if not windows:
        return "No windows are open (or Hyprland couldn't be asked)."
    lines = [f"{'* ' if w['focused'] else ''}{w['app']}: {w['title'][:70]} (workspace {w['workspace']})"
             for w in windows[:25]]
    return "Open windows (* = focused):\n" + "\n".join(lines)


def _dispatch(name, arg):
    """hyprctl dispatch, in both the plain and the quoted form (Lua configs)."""
    for form in (arg, f'"{arg}"'):
        out = _run(["hyprctl", "dispatch", name, form])
        text = (out.stdout + out.stderr).strip().lower()
        if out.returncode == 0 and (text in ("", "ok") or text.endswith("ok")) and "error" not in text:
            return True
    return False


def focus_window(match="", address=""):
    windows = list_windows()
    target = None
    if address:
        target = next((w for w in windows if w["address"] == address), None)
    elif match:
        m = match.lower()
        target = next((w for w in windows if m in w["app"].lower()), None) or \
            next((w for w in windows if m in w["title"].lower()), None)
    if target is None:
        return f"No open window matches '{match or address}'."
    if _dispatch("focuswindow", f"address:{target['address']}"):
        return f"Switched to {target['app']} ({target['title'][:50]})."
    return f"Hyprland wouldn't switch to {target['app']}."


# ---------------------------------------------------------------------------
# Programs
# ---------------------------------------------------------------------------

def list_programs(match=""):
    """Your running programs. With a match, only those whose name or command
    line contains it; without, the busiest ones."""
    out = _run(["ps", "-u", str(os.getuid()), "-o", "pid=,etimes=,pcpu=,rss=,comm=,args="])
    me = os.getpid()
    procs = []
    for line in out.stdout.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 5:
            continue
        try:
            pid, secs, cpu, rss = int(parts[0]), int(parts[1]), float(parts[2]), int(parts[3])
        except ValueError:
            continue
        name, args = parts[4], parts[5] if len(parts) > 5 else parts[4]
        if pid == me or name in ("ps",):
            continue
        procs.append({"pid": pid, "name": name, "running_s": secs, "cpu": cpu,
                      "memory_mb": round(rss / 1024), "command": args[:200]})
    if match:
        m = match.lower()
        procs = [p for p in procs if m in p["name"].lower() or m in p["command"].lower()]
        procs.sort(key=lambda p: -p["running_s"])
    else:
        procs.sort(key=lambda p: -p["cpu"])
        procs = procs[:15]
    return procs


def _span(seconds):
    h, rest = divmod(int(seconds), 3600)
    m = rest // 60
    return f"{h}h {m}m" if h else f"{m}m"


def describe_programs(match="", procs=None):
    procs = list_programs(match) if procs is None else procs
    if not procs:
        return f"Nothing matching '{match}' is running." if match else "Couldn't list programs."
    lines = [f"{p['name']} (pid {p['pid']}): running {_span(p['running_s'])}, CPU {p['cpu']:.0f}%, "
             f"{p['memory_mb']} MB" for p in procs[:12]]
    head = f"Running programs matching '{match}':" if match else "Busiest programs right now:"
    return head + "\n" + "\n".join(lines)


def stop_program(match):
    m = str(match or "").strip().lower()
    if len(m) < 3:
        return "Say which program to stop (at least three letters of its name)."
    parent = os.getppid()
    procs = [p for p in list_programs(m) if p["pid"] != parent]
    if any(marker in p["command"] for p in procs for marker in TOBY_MARKERS):
        return "That would stop Toby itself, so Toby won't. Use `toby stop` for that."
    if not procs:
        return f"Nothing called '{match}' is running."
    names = {p["name"] for p in procs}
    blocked = [n for n in names if n.lower() in PROTECTED_PROGRAMS]
    if blocked:
        return f"Toby won't stop {', '.join(sorted(blocked))}: your desktop depends on it."
    stopped = 0
    for p in procs:
        try:
            os.kill(p["pid"], signal.SIGTERM)
            stopped += 1
        except (ProcessLookupError, PermissionError):
            pass
    return f"Asked {', '.join(sorted(names))} to quit ({stopped} process{'es' if stopped != 1 else ''})."


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def _size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def list_files(path="~"):
    target = permissions.expand(path)
    if not target.exists():
        return f"{permissions.pretty(target)} doesn't exist."
    if target.is_file():
        st = target.stat()
        return f"{permissions.pretty(target)}: a file, {_size(st.st_size)}, modified {time.strftime('%d %b %H:%M', time.localtime(st.st_mtime))}."
    try:
        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as e:
        return f"Couldn't look in {permissions.pretty(target)}: {e.strerror}."
    _touch(target, "looked in")
    shown = [e for e in entries if not e.name.startswith(".")]
    lines = []
    for e in shown[:60]:
        try:
            lines.append(f"{e.name}/" if e.is_dir() else f"{e.name} ({_size(e.stat().st_size)})")
        except OSError:
            continue
    extra = f"\n…and {len(shown) - 60} more" if len(shown) > 60 else ""
    hidden = len(entries) - len(shown)
    note = f" ({hidden} hidden)" if hidden else ""
    return f"{permissions.pretty(target)} has {len(shown)} items{note}:\n" + "\n".join(lines) + extra


def search_files(query, path="~", limit=40, budget_s=3.0):
    """Files and folders whose name contains query (case doesn't matter)."""
    q = str(query or "").strip().lower()
    if not q:
        return "Say what to search for."
    root = permissions.expand(path)
    started = time.monotonic()
    found, truncated = [], False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in dirnames + filenames:
            if q in name.lower():
                found.append(Path(dirpath) / name)
                if len(found) >= limit:
                    break
        if len(found) >= limit or time.monotonic() - started > budget_s:
            truncated = True
            break
    if not found:
        return f"Nothing named like '{query}' in {permissions.pretty(root)}."
    found.sort(key=lambda p: -_mtime(p))
    lines = [permissions.pretty(p) + ("/" if p.is_dir() else "") for p in found]
    more = " (stopped early; there may be more)" if truncated else ""
    return f"Found {len(found)} matching '{query}'{more}, newest first:\n" + "\n".join(lines)


def _mtime(p):
    try:
        return p.stat().st_mtime
    except OSError:
        return 0


def read_file(path, limit=READ_LIMIT):
    target = permissions.expand(path)
    if not target.is_file():
        return f"{permissions.pretty(target)} isn't a file I can read."
    try:
        raw = target.read_bytes()[: limit + 1]
    except OSError as e:
        return f"Couldn't read {permissions.pretty(target)}: {e.strerror}."
    if b"\x00" in raw[:4000]:
        return f"{permissions.pretty(target)} is a binary file ({_size(target.stat().st_size)}); I can't read it as text."
    _touch(target, "read")
    text = raw[:limit].decode("utf-8", "replace")
    more = "\n[…the file continues]" if len(raw) > limit else ""
    return f"{permissions.pretty(target)}:\n{text}{more}"


def _refuse_outside_home(*paths):
    for p in paths:
        if not permissions.inside_home(p):
            return f"Toby only changes files inside your home folder, not {permissions.pretty(p)}."
    return None


def _backup(target):
    if target.is_file():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        copy = BACKUP_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{target.name}"
        shutil.copy2(target, copy)
        return copy
    return None


def write_file(path, content, append=False):
    target = permissions.expand(path)
    refused = _refuse_outside_home(target)
    if refused:
        return refused
    if target.is_dir():
        return f"{permissions.pretty(target)} is a folder, not a file."
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        backup = None if append else _backup(target)
        with open(target, "a" if append else "w", encoding="utf-8") as f:
            f.write(str(content))
    except OSError as e:
        return f"Couldn't write {permissions.pretty(target)}: {e.strerror}."
    _touch(target, "added to" if append else ("changed" if backup else "created"))
    note = f" (the old version is saved as {permissions.pretty(backup)})" if backup else ""
    return f"{'Added to' if append else 'Wrote'} {permissions.pretty(target)}{note}."


def move_file(source, dest):
    src, dst = permissions.expand(source), permissions.expand(dest)
    refused = _refuse_outside_home(src, dst)
    if refused:
        return refused
    if not src.exists():
        return f"{permissions.pretty(src)} doesn't exist."
    if dst.is_dir():
        dst = dst / src.name
    if dst.exists():
        return f"{permissions.pretty(dst)} already exists, so nothing was moved. Pick another name."
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    except OSError as e:
        return f"Couldn't move it: {e.strerror}."
    _touch(dst, "moved here")
    verb = "Renamed" if src.parent == dst.parent else "Moved"
    return f"{verb} {permissions.pretty(src)} to {permissions.pretty(dst)}."


def _trash_one(path):
    """Move one path to the freedesktop trash; gio if present, else by hand."""
    if shutil.which("gio"):
        out = _run(["gio", "trash", str(path)])
        if out.returncode == 0:
            return True
    trash = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local" / "share")) / "Trash"
    (trash / "files").mkdir(parents=True, exist_ok=True)
    (trash / "info").mkdir(parents=True, exist_ok=True)
    name, n = path.name, 1
    while (trash / "files" / name).exists():
        n += 1
        name = f"{path.stem}.{n}{path.suffix}"
    info = (f"[Trash Info]\nPath={urllib.parse.quote(str(path))}\n"
            f"DeletionDate={time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
    (trash / "info" / f"{name}.trashinfo").write_text(info)
    shutil.move(str(path), str(trash / "files" / name))
    return True


def trash_files(paths):
    targets = [permissions.expand(p) for p in (paths or [])]
    refused = _refuse_outside_home(*targets)
    if refused:
        return refused
    moved, missing = [], []
    for t in targets:
        if not t.exists():
            missing.append(permissions.pretty(t))
            continue
        try:
            if _trash_one(t):
                moved.append(t)
                _touch(t, "moved to trash")
        except OSError:
            missing.append(permissions.pretty(t))
    msg = f"Moved {len(moved)} item{'s' if len(moved) != 1 else ''} to the trash (restore them from there if needed)."
    if missing:
        msg += f" Couldn't find or move: {', '.join(missing[:5])}."
    return msg


def open_path(path):
    target = permissions.expand(path)
    if not target.exists():
        return f"{permissions.pretty(target)} doesn't exist."
    subprocess.Popen(["xdg-open", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    _touch(target, "opened")
    return f"Opened {permissions.pretty(target)}."


# ---------------------------------------------------------------------------
# Terminals, commands and downloads
# ---------------------------------------------------------------------------

TERMINALS = [
    ("kitty", lambda cwd, sh: ["kitty", "--directory", cwd] + sh),
    ("foot", lambda cwd, sh: ["foot", "-D", cwd] + sh),
    ("ghostty", lambda cwd, sh: ["ghostty", f"--working-directory={cwd}", "-e"] + sh),
    ("alacritty", lambda cwd, sh: ["alacritty", "--working-directory", cwd, "-e"] + sh),
    ("wezterm", lambda cwd, sh: ["wezterm", "start", "--cwd", cwd, "--"] + sh),
    ("konsole", lambda cwd, sh: ["konsole", "--workdir", cwd, "-e"] + sh),
    ("gnome-terminal", lambda cwd, sh: ["gnome-terminal", f"--working-directory={cwd}", "--"] + sh),
]


def terminal_command(cwd, command="", which=None):
    """The argv to open a terminal in cwd, optionally running command and
    then staying open at your shell. None if no known terminal is installed."""
    shell = os.environ.get("SHELL", "/bin/bash")
    sh = ["sh", "-c", f"{command}; exec {shell}"] if command else [shell]
    preferred = which or os.environ.get("TERMINAL", "")
    order = sorted(TERMINALS, key=lambda t: t[0] != os.path.basename(preferred))
    for name, build in order:
        if shutil.which(name):
            return build(str(cwd), sh)
    return None


def open_terminal(cwd="~", command=""):
    where = permissions.expand(cwd)
    if not where.is_dir():
        return f"{permissions.pretty(where)} isn't a folder."
    argv = terminal_command(where, command)
    if argv is None:
        return "No terminal I know how to open is installed (kitty, foot, alacritty, …)."
    subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                     cwd=str(where))
    return f"Opened a terminal in {permissions.pretty(where)}" + (f" running {command}." if command else ".")


def run_command(jobs, command, cwd="~", background=False, wait_s=45, task_id=None, cancel_check=None,
                name=None):
    """Run a shell command as a job. In the foreground, wait up to wait_s for
    it; if it's still going then, it carries on as a background job. If the
    task is cancelled while waiting, the command is stopped."""
    where = permissions.expand(cwd)
    if not where.is_dir():
        return f"{permissions.pretty(where)} isn't a folder.", None
    job = jobs.start(command, str(where), name=name, task_id=task_id)
    if background:
        return f"Started '{job.name}' in the background (job {job.id}).", job
    deadline = time.monotonic() + wait_s
    while not job.done.wait(min(0.25, max(0.0, deadline - time.monotonic()))):
        if cancel_check is not None and cancel_check():
            jobs.stop(job.id)
            job.done.wait(4)
            return f"Stopped '{job.name}' because the task was cancelled.", job
        if time.monotonic() >= deadline:
            break
    if not job.done.is_set():
        return (f"'{job.name}' is still running after {wait_s}s, so it carries on in the background "
                f"(job {job.id}). Latest output:\n" + "\n".join(job.tail(8))), job
    tail = "\n".join(job.tail(30))
    return f"{job.describe()}\nOutput (last lines):\n{tail}", job


def download_file(jobs, url, to="~/Downloads", task_id=None):
    parsed = urllib.parse.urlparse(str(url or ""))
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return "That isn't a web address I can download from.", None
    folder = permissions.expand(to)
    refused = _refuse_outside_home(folder)
    if refused:
        return refused, None
    name = os.path.basename(parsed.path) or "download"
    name = re.sub(r"[^\w.\-]+", "_", urllib.parse.unquote(name))[:120] or "download"
    if folder.suffix and not folder.is_dir():
        folder, name = folder.parent, folder.name
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / name
    n = 1
    while dest.exists():
        n += 1
        dest = folder / f"{Path(name).stem} ({n}){Path(name).suffix}"
    if not shutil.which("curl"):
        return "curl isn't installed, so Toby can't download files.", None
    import shlex
    command = f"curl -L --fail --progress-bar -o {shlex.quote(str(dest))} {shlex.quote(url)}"
    job = jobs.start(command, str(folder), name=f"Download {name}", task_id=task_id)
    _touch(dest, "downloading")
    return f"Downloading {name} to {permissions.pretty(folder)} (job {job.id}).", job
