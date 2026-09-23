#!/usr/bin/env python3
"""
Little Toby — an animated, local-first Linux desktop AI assistant.

A rainbow ring flash, a small animated chibi face that wakes up, and a
sliding textbox — all custom-drawn with Cairo, no external animation
libraries. Talks to a local Ollama model, keeps a personal knowledge graph
and study notes, can (with explicit per-session confirmation) control the
mouse/keyboard and install packages, and can shift into Study Mode on a
schedule or automatically when it recognizes schoolwork on screen.

Setup: ./install.sh, then `toby doctor`. See README.md.

Toby binds Super+G in the running Hyprland itself at startup (only if the
key is free, and never in your config). To bind it yourself instead:
  bind = SUPER, G, exec, pkill -SIGUSR1 -f linux_agent_apple.py

Optional glass blur behind Toby's surfaces, if you'd like it — add to your
Hyprland config:
  layerrule = blur, apple-agent
  layerrule = ignorezero, apple-agent
"""

import json
import math
import os
import re
import zlib
import subprocess
import threading
import time
import datetime
from datetime import datetime as dt
from enum import Enum, auto
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
from gi.repository import Gdk, GLib, Gtk, GtkLayerShell  # noqa: E402

import cairo  # noqa: E402
import requests  # noqa: E402

from screen_control import ScreenControl, NeedsConfirmation, press_keys
from screen_read import capture_screen_text
import knowledge
import study_notes
import study_plan
import school_mode
import toby_settings
import voice_engine
import camera_engine
import chibi
import fast_path
import fingerprint
import hypr_events
import hypr_keybind
import hypr_animations
import model_picker
import remote_bridge
import toby_anim

SETTINGS = toby_settings.load()

REPO_DIR = Path.home() / "linux-agent"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/chat")
# Blank means "pick the best installed model at startup" (model_picker.py).
OLLAMA_MODEL_ENV = os.environ.get("OLLAMA_MODEL", "").strip()
OLLAMA_MODEL = OLLAMA_MODEL_ENV or model_picker.FALLBACK
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
EMAIL_ADDRESS = os.environ.get("EMAIL_ADDRESS")
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD")
IMAP_SERVER = os.environ.get("IMAP_SERVER", "imap.gmail.com")

HISTORY_PATH = REPO_DIR / "history.json"
SESSION_LOG_PATH = REPO_DIR / "session_log.md"

FPS_MS = 16  # ~60fps tick
IDLE_TIMEOUT_S = 25  # how long with no interaction before it "falls asleep"

# The prompt is deliberately split in two. Everything in SYSTEM_PROMPT_STATIC
# is byte-for-byte identical on every request, which lets Ollama reuse the
# cached attention state for it instead of reprocessing a couple of thousand
# tokens each time. Everything that changes between requests — the date, the
# focused window, saved facts and notes, the user's style settings — goes in
# a block appended after it. This ordering is the single cheapest speed win
# available on CPU-only hardware, where re-reading the prompt is most of the
# wait. Putting anything volatile in the middle of the static half throws the
# whole cached prefix away, so keep new context in the tail block.
SYSTEM_PROMPT_STATIC = """You are Little Toby, a task-doing desktop \
assistant running locally on Linux (Hyprland window manager). Your primary job is to actually \
DO things via actions, not just describe them — when a message implies a desktop task ("open X",
"send Y", "close this", "check my email", "install Z"), call the matching tool immediately rather
than explaining how the user could do it themselves. Only skip acting and ask a clarifying
question when the request is genuinely ambiguous about which target/app/value to use. You can
still have plain conversations and answer questions with no actions at all — not everything is a
task. CRITICAL: a factual/knowledge question ("how old is X", "what's the capital of Y", "who
directed Z") is NEVER a desktop task, even if you're not fully certain of the answer — answer it
directly from what you know, in "reply", with an empty "actions" list. Do NOT open a browser,
search a site, or call read_screen to "look up" the answer — you have no search tool, and
open_url just opens a blank results page without ever reading it, which helps no one. If you
genuinely don't know, say so plainly in "reply" rather than opening something that can't answer
it either. Respond with ONLY a JSON object, no prose, no markdown fences, shaped exactly like:
{{"actions": [...], "reply": "...", "mood": "happy|neutral|concerned|excited"}}

"actions" is an ordered list of desktop commands (empty if just chatting) — always nest it
inside actions, even for a single action. Each is one of:
  {{"tool": "open_url", "url": "https://..."}}
  {{"tool": "open_app", "command": "app-executable-name"}}
  {{"tool": "send_discord_message", "content": "message text"}}
  {{"tool": "close_active_window"}}
  {{"tool": "close_tab"}}
  {{"tool": "read_emails", "from_contains": "", "subject_contains": "", "count": 5, "query": ""}}
    -- "query" uses real Gmail search syntax (e.g. "from:boss after:2026/01/01
       has:attachment subject:invoice") and overrides from_contains/subject_contains
       when set. Use query for anything beyond a simple sender/subject match.
  {{"tool": "move_mouse", "x": 0.5, "y": 0.5}}
  {{"tool": "click_mouse", "button": "left"}}
  {{"tool": "type_text", "text": "hello"}}
  {{"tool": "key_press", "keys": "ctrl+c"}}
  {{"tool": "disable_control"}}
  {{"tool": "read_screen"}}
  {{"tool": "install_package", "package": "name"}}
  {{"tool": "show_knowledge_tree"}}
  {{"tool": "show_knowledge_bubbles"}}
  {{"tool": "add_study_note", "subject": "ELA", "topic": "Annie John - Chapter 2", "content": "..."}}
  {{"tool": "enable_study_mode"}}
  {{"tool": "disable_study_mode"}}
  {{"tool": "set_school_schedule", "day": "monday", "start": "07:30", "end": "19:00"}}

"reply" is shown to the user. "mood" reflects how the reply should feel — pick whichever fits.

Rules:
- Site name ("youtube", "tiktok") -> open_url with https:// homepage.
- Native app name -> open_app.
- Coding questions -> show code/explanation in "reply", never write files.
- Preserve multi-step order.
- Only use these exact tool names, never invent new ones: open_url, open_app,
  send_discord_message, close_active_window, close_tab, read_emails, move_mouse,
  click_mouse, type_text, key_press, disable_control, read_screen, install_package,
  show_knowledge_tree, show_knowledge_bubbles, add_study_note, enable_study_mode,
  disable_study_mode, set_school_schedule.
- x and y for move_mouse are fractions of the screen from 0.0 to 1.0, never raw pixels.
- read_screen: ONLY call this when the user's message explicitly references
  something currently visible on their screen — "read this", "what does this
  say", "summarize this page", "what's this error". Do NOT call it for
  greetings, small talk, general knowledge questions, or anything that
  doesn't require seeing the screen to answer. Example: "hello" -> no
  actions at all, just reply. Example: "what does this error mean" -> call
  read_screen first. When in doubt, do NOT call it — a wrong guess costs a
  slow, unnecessary screen capture for no reason. You CANNOT see the screen
  unless you call this tool — never claim to know screen content you
  haven't actually read this turn.
- close_active_window closes the WHOLE window/app. close_tab closes just the current
  tab (Ctrl+W) — use this when the user means a browser tab, not the entire window.
  If ambiguous, prefer close_tab since it's less disruptive.
- Mouse/keyboard control and package installs require the user to click a one-time
  confirmation the first time in a session — never claim to have moved the mouse,
  clicked, typed, or installed something without actually calling the tool. Always
  requires fresh confirmation. Never claim to have installed something without
  actually calling this tool.
- show_knowledge_tree / show_knowledge_bubbles: call when the user asks what you
  know about them, or asks to see their knowledge graph/tree of facts. These open
  image files, they don't return text — just confirm you're showing it.
- add_study_note: use when the user explicitly asks you to save/remember something
  for studying, or during a genuine review conversation where they're working through
  material. Do NOT use this to store homework answers you generated for them — only
  concepts, summaries, and things they're actually learning. When asked to quiz,
  summarize, or make flashcards, use the study notes already in context above — pull
  from real saved notes, never invent content that isn't there.
- enable_study_mode / disable_study_mode: toggle when the user explicitly asks to
  start or stop studying. Shows a visible badge in the sidebar — never claim it's
  on without actually calling the tool.
- set_school_schedule: when the user describes their school/homework schedule
  conversationally, convert it to 24-hour HH:MM times and call this once per day
  mentioned (e.g. a Mon-Fri schedule with a different Wednesday needs 5 separate
  calls). Days with no schedule set are simply inactive for auto-detection.
- If you don't have a tool for something, say so plainly instead of inventing one.
"""

# The block above is still written with doubled braces from the days when it
# was a format template. Unescape it once here, not on every request.
SYSTEM_PROMPT_STATIC = SYSTEM_PROMPT_STATIC.format()

# Hard ceilings on the parts of the prompt that grow without limit. Saved
# facts and study notes accumulate forever, and on CPU-only hardware every
# extra thousand characters of prompt is felt directly as a longer wait
# before the first word appears.
MAX_FACTS_CHARS = 900
MAX_NOTES_CHARS = 1400
MAX_HISTORY_TURNS = 8
MAX_HISTORY_MESSAGE_CHARS = 700

# Ask Ollama to keep the model resident between requests. The default is to
# unload after five minutes idle, which means the first thing said after a
# short break pays to load several gigabytes from disk again — by far the
# worst latency in ordinary use, and entirely avoidable.
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")

# Replies are a few sentences plus a short action list. Capping the output
# stops a model that starts rambling from holding the turn open for minutes,
# and low temperature keeps tool-call JSON well formed.
OLLAMA_OPTIONS = {"temperature": 0.2, "top_p": 0.9, "num_predict": 600}


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

ALLOWED_APPS = {
    "firefox": "firefox",
    "chrome": "google-chrome-stable",
    "terminal": "kitty",
    "kitty": "kitty",
    "code": "code",
    "vscode": "code",
    "spotify": "spotify",
    "discord": "discord",
    "steam": "steam",
    "files": "thunar",
    "nautilus": "nautilus",
    "gimp": "gimp",
    "libreoffice": "libreoffice",
    "vlc": "vlc",
    "blender": "blender",
    "obs": "obs",
    # Add more here if you trust the AI to launch them by name.
}

# Empty by default — nothing is installable until you explicitly add package
# names here that you trust the AI to suggest. Installs also always require
# fresh confirmation each time (unlike mouse/keyboard, which stays granted
# for the session) and open a VISIBLE terminal where YOU type your own
# password — the AI never runs sudo directly.
ALLOWED_PACKAGES = {
    # "htop", "neofetch", ...
}


def open_url(url):
    if not url.startswith("http"):
        url = "https://" + url
    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"Opened {url}"


def open_app(command):
    key = command.strip().lower()
    real_command = ALLOWED_APPS.get(key)
    if not real_command:
        return (f"'{command}' isn't in the allowed apps list — add it to "
                f"ALLOWED_APPS in the script if you trust launching it by name.")
    subprocess.Popen(real_command.split(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"Launched {real_command}"


def send_discord_message(content):
    if not DISCORD_WEBHOOK_URL:
        return "No DISCORD_WEBHOOK_URL configured."
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=10)
    return "Sent." if resp.status_code in (200, 204) else f"Discord failed ({resp.status_code})"


def close_active_window():
    # Some Hyprland configs (Lua-based dispatch configs in particular) reject
    # the standard "killactive" string dispatcher. Super+Q is bound to
    # window-close in most user configs, so fall back to simulating that
    # keypress via ydotool if the direct dispatch fails.
    try:
        result = subprocess.run(["hyprctl", "dispatch", "killactive"], capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            return "Closed."
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    result = press_keys("super+q")
    if result.startswith("Pressed"):
        return "Closed (via Super+Q simulation)."
    return f"Couldn't close the window. {result}"


def close_tab():
    result = press_keys("ctrl+w")
    return "Closed tab (Ctrl+W)." if result.startswith("Pressed") else result


def read_emails(from_contains="", subject_contains="", count=5, query=""):
    if not (EMAIL_ADDRESS and EMAIL_APP_PASSWORD):
        return "Email isn't configured."
    import email as email_lib
    import imaplib
    from email.header import decode_header

    try:
        m = imaplib.IMAP4_SSL(IMAP_SERVER)
        m.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
        m.select("INBOX")
        if query:
            # Native Gmail search syntax (e.g. "from:boss after:2026/01/01
            # has:attachment"), via Gmail's IMAP X-GM-RAW extension. Only
            # works against Gmail's servers, not generic IMAP.
            typ, data = m.search(None, "X-GM-RAW", f'"{query}"')
        else:
            criteria = []
            if from_contains:
                criteria += ["FROM", f'"{from_contains}"']
            if subject_contains:
                criteria += ["SUBJECT", f'"{subject_contains}"']
            typ, data = m.search(None, " ".join(criteria) if criteria else "ALL")
        ids = data[0].split()[-count:] if data[0] else []
        if not ids:
            m.logout()
            return "No matching emails."
        lines = []
        for msg_id in reversed(ids):
            typ, msg_data = m.fetch(msg_id, "(RFC822)")
            msg = email_lib.message_from_bytes(msg_data[0][1])

            def dec(v):
                if not v:
                    return ""
                parts = decode_header(v)
                return "".join(t.decode(e or "utf-8", errors="replace") if isinstance(t, bytes) else t for t, e in parts)

            lines.append(f"From: {dec(msg.get('From'))} | Subject: {dec(msg.get('Subject'))}")
        m.logout()
        return "\n".join(lines)
    except Exception as e:
        return f"Email check failed: {e}"


class InstallGuard:
    def __init__(self):
        self.enabled = False

    def grant_once(self):
        self.enabled = True

    def consume(self):
        was = self.enabled
        self.enabled = False
        return was


install_guard = InstallGuard()


def install_package(package_name):
    pkg = package_name.strip().lower()
    if pkg not in ALLOWED_PACKAGES:
        return (f"'{pkg}' isn't in the allowed packages list — add it to "
                f"ALLOWED_PACKAGES in the script first if you trust it.")
    if not install_guard.consume():
        raise NeedsConfirmation()
    subprocess.Popen(["kitty", "fish", "-c", f"sudo pacman -S {pkg}"])
    return (f"Opened a terminal with 'sudo pacman -S {pkg}' — review it and "
            f"enter your own password there to actually install it.")


screen_control = ScreenControl()


# ---------------------------------------------------------------------------
# Pointer driver — used by Camera Mode's pinch-to-point
#
# Hand tracking produces a new pointer target every camera frame, and every
# pointer move is a subprocess call that can block for its full timeout when
# ydotoold isn't running. Doing that inline would stall the camera loop; doing
# it on the GTK thread would stall the interface. So moves happen on a thread
# of their own that only ever cares about the most recent target — if frames
# arrive faster than the pointer can be moved, the stale ones are dropped
# rather than queued, which is what keeps the cursor tracking the hand instead
# of lagging behind it.
# ---------------------------------------------------------------------------

class PointerDriver:
    MIN_INTERVAL_S = 0.04  # ~25 pointer updates a second is plenty

    def __init__(self, control):
        self._control = control
        self._lock = threading.Lock()
        self._target = None
        self._click_pending = False
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self.last_error = ""

    def stopped(self):
        return self._stop.is_set()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        with self._lock:
            self._target = None
            self._click_pending = False

    def aim(self, x_frac, y_frac):
        """Set where the pointer should be. The newest call wins."""
        with self._lock:
            self._target = (x_frac, y_frac)
        self._wake.set()

    def request_click(self):
        with self._lock:
            self._click_pending = True
        self._wake.set()

    def _run(self):
        last_move = 0.0
        while not self._stop.is_set():
            self._wake.wait(0.2)
            self._wake.clear()
            if self._stop.is_set():
                return
            with self._lock:
                target = self._target
                self._target = None
                wants_click = self._click_pending
                self._click_pending = False
            try:
                if target is not None and time.monotonic() - last_move >= self.MIN_INTERVAL_S:
                    outcome = self._control.move_immediate(*target)
                    last_move = time.monotonic()
                    if outcome != "ok":
                        self.last_error = "Couldn't move the pointer — is ydotoold running?"
                        self._stop.set()
                        return
                if wants_click:
                    self._control.click("left")
            except NeedsConfirmation:
                # consent was withdrawn mid-session; stand down quietly
                self.last_error = "Mouse control is off."
                self._stop.set()
                return
            except Exception as e:
                self.last_error = str(e)
                self._stop.set()
                return


pointer_driver = PointerDriver(screen_control)

last_screen_context = ""


class StudyModeState:
    def __init__(self):
        self.active = False
        self.badge = None   # legacy hook, unused now that Dashboard shows status
        self.helper = None  # StudyHelper window, set once AssistantWindow builds it
        self.session_stats = {"summaries": 0, "quizzes_taken": 0, "flashcards_reviewed": 0, "session_start": None}

    def enable(self):
        self.active = True
        if self.session_stats["session_start"] is None:
            self.session_stats["session_start"] = time.monotonic()
        if self.helper:
            GLib.idle_add(self.helper.open_helper)
        return "Study Mode enabled."

    def disable(self):
        self.active = False
        self.session_stats["session_start"] = None
        if self.helper:
            GLib.idle_add(self.helper.close_helper)
        return "Study Mode disabled."


study_mode_state = StudyModeState()


DISPATCH = {
    "open_url": lambda a: open_url(a["url"]),
    "open_app": lambda a: open_app(a["command"]),
    "send_discord_message": lambda a: send_discord_message(a["content"]),
    "close_active_window": lambda a: close_active_window(),
    "close_tab": lambda a: close_tab(),
    "read_emails": lambda a: read_emails(
        a.get("from_contains", ""), a.get("subject_contains", ""), a.get("count", 5), a.get("query", "")
    ),
    "move_mouse": lambda a: screen_control.move(a.get("x", 0.5), a.get("y", 0.5)),
    "click_mouse": lambda a: screen_control.click(a.get("button", "left")),
    "type_text": lambda a: screen_control.type_text(a.get("text", "")),
    "key_press": lambda a: screen_control.key(a.get("keys", "")),
    "disable_control": lambda a: screen_control.disable(),
    "install_package": lambda a: install_package(a["package"]),
    "show_knowledge_tree": lambda a: knowledge.render_tree(),
    "show_knowledge_bubbles": lambda a: knowledge.render_bubbles(),
    "add_study_note": lambda a: study_notes.add_note(a["subject"], a["topic"], a["content"]),
    "enable_study_mode": lambda a: study_mode_state.enable(),
    "disable_study_mode": lambda a: study_mode_state.disable(),
    "set_school_schedule": lambda a: school_mode.set_day_schedule(a["day"], a["start"], a["end"]),
}


def describe_action(action):
    """A short, plain label for one step — what the checklist shows.

    The model's tool names ("open_url", "key_press") mean nothing to anyone
    reading over Toby's shoulder; "Open youtube.com" does.
    """
    tool = action.get("tool", "")
    def short(text, n=34):
        text = str(text or "").strip()
        return text if len(text) <= n else text[: n - 1] + "…"

    if tool == "open_url":
        url = str(action.get("url", "")).replace("https://", "").replace("http://", "")
        return f"Open {short(url.rstrip('/'))}"
    if tool == "open_app":
        return f"Open {short(action.get('command', 'an app'))}"
    if tool == "type_text":
        return f'Type "{short(action.get("text", ""), 26)}"'
    if tool == "key_press":
        return f"Press {short(action.get('keys', '')).upper()}"
    if tool == "click_mouse":
        button = action.get("button", "left")
        return "Click" if button == "left" else f"{button.capitalize()}-click"
    if tool == "move_mouse":
        return "Move the pointer"
    labels = {
        "send_discord_message": "Send the Discord message",
        "close_active_window": "Close this window",
        "close_tab": "Close this tab",
        "read_emails": "Check your email",
        "read_screen": "Read your screen",
        "install_package": f"Install {short(action.get('package', 'a package'))}",
        "show_knowledge_tree": "Show what I know about you",
        "show_knowledge_bubbles": "Show what I know about you",
        "add_study_note": f"Save a {short(action.get('subject', 'study'), 16)} note",
        "enable_study_mode": "Turn on Study Mode",
        "disable_study_mode": "Turn off Study Mode",
        "set_school_schedule": f"Set {short(action.get('day', ''), 12).capitalize()}'s schedule",
        "disable_control": "Hand back the mouse and keyboard",
    }
    return labels.get(tool, tool.replace("_", " ").capitalize() or "Step")


# Tools where Toby visibly acts on the screen. For these the chibi comes out
# and does the work in view; for purely informational ones (checking email,
# saving a note) there's nothing on screen to walk to.
PHYSICAL_TOOLS = {"move_mouse", "click_mouse", "type_text", "key_press", "open_url",
                  "open_app", "close_tab", "close_active_window", "send_discord_message"}


def load_history():
    if not HISTORY_PATH.exists():
        return []
    try:
        return json.loads(HISTORY_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def save_history(history):
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        HISTORY_PATH.write_text(json.dumps(history[-200:], indent=2))
    except OSError as e:
        print("HISTORY SAVE ERROR:", e, flush=True)


def log_session(user_text, assistant_reply):
    try:
        SESSION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        timestamp = dt.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"### {timestamp}\n\n**You:** {user_text}\n\n**Assistant:** {assistant_reply}\n\n---\n\n"
        with open(SESSION_LOG_PATH, "a") as f:
            f.write(entry)
    except OSError as e:
        print("SESSION LOG ERROR:", e, flush=True)


_last_cpu_times = None  # (idle, total) from the previous sample, for delta-based % calc


def sample_cpu_percent():
    """Delta-based CPU usage since the last call (returns None on the very
    first call, since there's nothing to diff against yet). Pure /proc
    parsing — no psutil dependency needed for one number."""
    global _last_cpu_times
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()[1:]
        nums = [int(x) for x in parts]
        idle = nums[3] + nums[4]
        total = sum(nums)
    except Exception:
        return None
    if _last_cpu_times is None:
        _last_cpu_times = (idle, total)
        return None
    prev_idle, prev_total = _last_cpu_times
    _last_cpu_times = (idle, total)
    d_idle = idle - prev_idle
    d_total = total - prev_total
    if d_total <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * (1.0 - d_idle / d_total)))


def sample_mem_percent():
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                info[key.strip()] = int(rest.strip().split()[0])
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", total)
        if total <= 0:
            return None
        return max(0.0, min(100.0, 100.0 * (1.0 - avail / total)))
    except Exception:
        return None


def count_todays_sessions():
    if not SESSION_LOG_PATH.exists():
        return 0
    today_str = dt.now().strftime("%Y-%m-%d")
    try:
        with open(SESSION_LOG_PATH) as f:
            return sum(1 for line in f if line.startswith(f"### {today_str}"))
    except OSError:
        return 0


def get_active_window_context():
    """Cheap, always-on ambient awareness: which app/window is focused right now.
    No screenshot, no OCR, no confirmation needed — just window title/class via
    Hyprland's own IPC. This lets Toby act sensibly ("close this", "what's this
    app") without a separate read_screen round-trip for the common case."""
    try:
        out = subprocess.run(
            ["hyprctl", "-j", "activewindow"], capture_output=True, text=True, timeout=1
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        data = json.loads(out.stdout)
        title = data.get("title", "")
        wclass = data.get("class", "")
        if not (title or wclass):
            return None
        return f"{wclass}: {title}" if wclass else title
    except Exception:
        return None


RECENT_ACTIONS_MAXLEN = 6
VOICE_CONFIDENCE_THRESHOLD = 0.55  # below this, Vosk's guess is too shaky to act on
VOICE_MIN_WORDS = 2                # a single stray word ("the", "so") is usually noise, not a command
recent_actions = []  # short rolling log of {"tool":..., "summary":...} for continuity across turns


def note_recent_action(tool, summary):
    recent_actions.append({"tool": tool, "summary": summary})
    del recent_actions[:-RECENT_ACTIONS_MAXLEN]


_REPLY_FIELD_RE = re.compile(r'"reply"\s*:\s*"((?:[^"\\]|\\.)*)')


def extract_partial_reply(raw):
    """Pulls the growing value of the "reply" field out of a streaming JSON
    string that isn't valid/complete JSON yet. Used both to make the on-screen
    answer stream progressively (instead of only appearing once the whole
    JSON response finishes) and to know how much of the reply is safe to
    speak aloud so far in Voice Mode. Returns None if "reply" hasn't started
    yet, or the model isn't producing JSON-shaped output at all."""
    m = _REPLY_FIELD_RE.search(raw)
    if not m:
        return None
    return _unescape_json_string(m.group(1))


_JSON_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f",
                 '"': '"', "\\": "\\", "/": "/"}


def _unescape_json_string(text):
    """Decode JSON string escapes in one left-to-right pass.

    Doing it as a series of str.replace calls gets the order wrong: an
    escaped backslash followed by an "n" would first be read as a newline,
    so a Windows path or a regex in a reply came out mangled.
    """
    out = []
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        if ch != "\\" or i + 1 >= length:
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1]
        if nxt == "u" and i + 5 < length:
            try:
                out.append(chr(int(text[i + 2:i + 6], 16)))
                i += 6
                continue
            except ValueError:
                pass
        out.append(_JSON_ESCAPES.get(nxt, nxt))
        i += 2
    return "".join(out)


def _clip(text, limit, what):
    """Trim a context block to a character budget, keeping the most recent
    lines, and say so rather than silently dropping things."""
    if not text or len(text) <= limit:
        return text
    lines = text.split("\n")
    header, body = lines[0], lines[1:]
    kept = []
    used = len(header)
    for line in reversed(body):
        if used + len(line) + 1 > limit:
            break
        kept.append(line)
        used += len(line) + 1
    kept.reverse()
    omitted = len(body) - len(kept)
    suffix = f"\n({omitted} older {what} not shown)" if omitted else ""
    return "\n".join([header] + kept) + suffix


def _build_volatile_context():
    """Everything that can differ from one request to the next.

    Appended after the static prompt so the static half stays byte-identical
    and can be served from Ollama's cached prefix. Order inside this block
    doesn't matter for caching, only that nothing volatile leaks earlier.
    """
    parts = ["Today's date is " + datetime.date.today().strftime("%B %d, %Y") + "."]

    parts.append(toby_settings.style_prompt_line(SETTINGS))
    grade = SETTINGS.get("grade_level", "9th-10th")
    parts.append(f"When explaining or summarizing academic material, pitch it at a "
                 f"{grade} level of complexity.")

    context_lines = []
    active_window = get_active_window_context()
    if active_window:
        context_lines.append(f"Currently focused window: {active_window}")
    if recent_actions:
        recent_str = "; ".join(f"{a['tool']} ({a['summary']})" for a in recent_actions)
        context_lines.append(f"Actions taken earlier this session (most recent last): {recent_str}")
    if context_lines:
        parts.append("Ambient context (for your awareness, not something the user necessarily "
                     "mentioned):\n" + "\n".join(context_lines))

    facts_context = _clip(knowledge.get_facts_summary(), MAX_FACTS_CHARS, "facts")
    if facts_context:
        parts.append(facts_context)
    notes_context = _clip(study_notes.get_notes_summary(), MAX_NOTES_CHARS, "notes")
    if notes_context:
        parts.append(notes_context)

    return "\n\n".join(parts)


def _build_system_content():
    return SYSTEM_PROMPT_STATIC + "\n\n" + _build_volatile_context()


def _trim_history(history):
    """The last few turns, with any single long message shortened.

    One pasted error log in the history would otherwise be re-read in full on
    every subsequent request for the rest of the session.
    """
    trimmed = []
    for message in history[-MAX_HISTORY_TURNS:]:
        content = message.get("content", "")
        if len(content) > MAX_HISTORY_MESSAGE_CHARS:
            content = content[:MAX_HISTORY_MESSAGE_CHARS] + " […trimmed]"
        trimmed.append({"role": message.get("role", "user"), "content": content})
    return trimmed


def _active_model():
    return SETTINGS.get("ollama_model") or OLLAMA_MODEL


def apply_model_settings():
    """Point the helper modules at whatever model/endpoint is configured now.

    Called at startup and again whenever Settings are saved, so choosing a
    lighter model for speed actually applies to fact extraction, note
    extraction, quizzes, flashcards and screen summaries too — not just to
    the conversation.
    """
    model = _active_model()
    knowledge.configure(OLLAMA_URL, model, OLLAMA_KEEP_ALIVE)
    study_notes.configure(OLLAMA_URL, model, OLLAMA_KEEP_ALIVE)


def resolve_model():
    """Settle which local model to use, from what Ollama has installed."""
    global OLLAMA_MODEL
    OLLAMA_MODEL = model_picker.pick(SETTINGS.get("ollama_model", ""), OLLAMA_MODEL_ENV,
                                     model_picker.installed_models(OLLAMA_URL))
    return OLLAMA_MODEL


def warm_up_model():
    """Load the model into memory in the background at startup.

    Ollama loads a model on first use and unloads it again after an idle
    period. On CPU-only hardware that load is several seconds of silence
    before the very first answer, which reads as the app being broken. This
    asks for a zero-token generation, whose only purpose is the side effect
    of having the model resident by the time it is first needed.
    """
    def worker():
        try:
            resolve_model()
            apply_model_settings()
            requests.post(
                OLLAMA_URL.replace("/api/chat", "/api/generate"),
                json={"model": _active_model(), "prompt": "", "stream": False,
                      "keep_alive": OLLAMA_KEEP_ALIVE},
                timeout=180,
            )
        except Exception:
            pass  # Ollama may not be running yet; the first real request will say so

    threading.Thread(target=worker, daemon=True).start()


_last_model_check = [0.0]


def _maybe_upgrade_model():
    """If a better model finished downloading in the background, start
    using it. Checked at most every two minutes, on the request thread."""
    if SETTINGS.get("ollama_model") or OLLAMA_MODEL_ENV:
        return
    if time.monotonic() - _last_model_check[0] < 120:
        return
    _last_model_check[0] = time.monotonic()
    before = OLLAMA_MODEL
    if resolve_model() != before:
        apply_model_settings()
        print(f"MODEL: switched to {OLLAMA_MODEL} now that it's installed", flush=True)


def think(instruction, history, on_chunk=None, cancel_check=None):
    _maybe_upgrade_model()
    system_content = _build_system_content()
    messages = [{"role": "system", "content": system_content}]
    messages.extend(_trim_history(history))
    messages.append({"role": "user", "content": instruction})
    payload = {
        "model": _active_model(),
        "messages": messages,
        "stream": bool(on_chunk),
        "format": "json",
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "options": OLLAMA_OPTIONS,
    }
    if model_picker.is_hybrid_thinker(payload["model"]):
        # these reason at length before answering unless told not to
        payload["think"] = False

    # 300s, not 120s — a longer, multi-step request (or a longer context from
    # facts/notes/ambient window info) can genuinely take a while to even
    # start streaming on CPU-bound hardware. This was very likely the actual
    # cause of "it keeps cancelling" on anything more involved than a short
    # reply: a plain requests.exceptions.Timeout wasn't even being caught by
    # name before, so it surfaced as a generic, confusing "Ollama error".
    OLLAMA_TIMEOUT = 300
    try:
        if on_chunk:
            resp = requests.post(OLLAMA_URL, json=payload, timeout=OLLAMA_TIMEOUT, stream=True)
            if resp.status_code == 400 and "think" in payload and "think" in resp.text.lower():
                # an older Ollama that doesn't know the option: ask again without it
                payload.pop("think")
                resp = requests.post(OLLAMA_URL, json=payload, timeout=OLLAMA_TIMEOUT, stream=True)
            if resp.status_code == 404:
                return {"actions": [], "reply": (
                    f"The model {payload['model']} isn't installed. Run: ollama pull {payload['model']}"
                    " — or pick another model in Settings."), "mood": "concerned"}
            resp.raise_for_status()
            raw = ""
            for line in resp.iter_lines():
                if cancel_check and cancel_check():
                    resp.close()
                    return {"actions": [], "reply": "", "mood": "neutral", "_cancelled": True}
                if not line:
                    continue
                try:
                    piece = json.loads(line)
                except json.JSONDecodeError:
                    continue
                delta = piece.get("message", {}).get("content", "")
                if delta:
                    raw += delta
                    try:
                        on_chunk(raw)
                    except Exception:
                        pass
                if piece.get("done"):
                    break
        else:
            resp = requests.post(OLLAMA_URL, json=payload, timeout=OLLAMA_TIMEOUT)
            resp.raise_for_status()
            raw = resp.json()["message"]["content"]
    except requests.exceptions.Timeout:
        return {"actions": [], "reply": (
            f"Ollama didn't respond within {OLLAMA_TIMEOUT}s — that's a genuinely long request for your "
            "hardware (multi-step instructions take longer to generate). Try breaking it into smaller "
            "steps, or switch to a lighter model in Settings (e.g. qwen2.5:1.5b)."
        ), "mood": "concerned"}
    except requests.exceptions.ConnectionError:
        return {"actions": [], "reply": "Can't reach Ollama — is it running?", "mood": "concerned"}

    return _parse_llm_json_reply(raw)


def _parse_llm_json_reply(raw):
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"actions": [], "reply": raw[:600], "mood": "neutral"}
    parsed.setdefault("actions", [])
    parsed.setdefault("reply", "")
    parsed.setdefault("mood", "neutral")
    return parsed


def think_cloud(instruction, history, on_chunk=None, cancel_check=None):
    """Optional cloud fallback for tough requests — OpenAI only for now, opt-in,
    off by default. Never used unless the user explicitly enables it and
    provides their own API key in Settings; the key is read from local
    settings only and sent to nowhere but api.openai.com."""
    api_key = SETTINGS.get("cloud_api_key", "").strip()
    if not api_key:
        return {"actions": [], "reply": "No cloud API key set — add one in Settings first.", "mood": "concerned"}

    system_content = _build_system_content()
    messages = [{"role": "system", "content": system_content}]
    messages.extend(_trim_history(history))
    messages.append({"role": "user", "content": instruction})
    model = SETTINGS.get("cloud_model") or "gpt-4o"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model, "messages": messages,
        "response_format": {"type": "json_object"},
        "stream": bool(on_chunk),
    }

    try:
        if on_chunk:
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers=headers, json=payload, timeout=60, stream=True,
            )
            if resp.status_code != 200:
                return {"actions": [], "reply": f"Cloud AI error ({resp.status_code}): {resp.text[:200]}",
                        "mood": "concerned"}
            raw = ""
            for line in resp.iter_lines(decode_unicode=True):
                if cancel_check and cancel_check():
                    resp.close()
                    return {"actions": [], "reply": "", "mood": "neutral", "_cancelled": True}
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[len("data: "):]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    piece = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                delta = piece.get("choices", [{}])[0].get("delta", {}).get("content", "")
                if delta:
                    raw += delta
                    try:
                        on_chunk(raw)
                    except Exception:
                        pass
        else:
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers=headers, json=payload, timeout=60,
            )
            if resp.status_code != 200:
                return {"actions": [], "reply": f"Cloud AI error ({resp.status_code}): {resp.text[:200]}",
                        "mood": "concerned"}
            raw = resp.json()["choices"][0]["message"]["content"]
    except requests.exceptions.Timeout:
        return {"actions": [], "reply": "The cloud AI didn't respond in time — try again in a moment.",
                "mood": "concerned"}
    except requests.exceptions.RequestException as e:
        return {"actions": [], "reply": f"Couldn't reach the cloud AI: {e}", "mood": "concerned"}

    return _parse_llm_json_reply(raw)

# ---------------------------------------------------------------------------
# Animation helpers
# ---------------------------------------------------------------------------

def ease_out_back(t):
    c1 = 1.70158
    c3 = c1 + 1
    t = max(0.0, min(1.0, t))
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


def ease_out_cubic(t):
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def lerp(a, b, t):
    return a + (b - a) * t


class State(Enum):
    SLEEPING = auto()
    WAKING = auto()
    IDLE = auto()
    LISTENING = auto()
    THINKING = auto()
    RESPONDING = auto()
    SPEAKING = auto()

MOOD_TO_STATE_TWEAK = {
    "happy": dict(mouth=0.8, eyebrow=0.2),
    "excited": dict(mouth=1.0, eyebrow=0.5),
    "concerned": dict(mouth=-0.5, eyebrow=-0.3),
    "neutral": dict(mouth=0.3, eyebrow=0.0),
}

# ---------------------------------------------------------------------------
# The face — custom Cairo drawing, driven by a state machine
# ---------------------------------------------------------------------------

class Face(Gtk.DrawingArea):
    # The face itself is 40px across; the widget is a little larger so the
    # thinking motes and the squash on a tap have room to move without being
    # clipped at the edges.
    FACE_DIAMETER = 40
    SIZE = 56

    def __init__(self):
        super().__init__()
        self.set_size_request(self.SIZE, self.SIZE)
        self.connect("draw", self.on_draw)

        # physical reactions: a squash when tapped, a hop when a task is done,
        # a glance when the desktop changes — all springs, so a second tap
        # mid-reaction continues the motion instead of restarting it
        self.squash = toby_anim.Spring(0.0, stiffness=420, damping=16)
        self.glance = toby_anim.Spring(0.0, stiffness=90, damping=14)
        self.brow_react = toby_anim.Spring(0.0, stiffness=120, damping=14)
        self._last_tick_time = time.monotonic()
        self.anim = toby_anim.animation_settings(SETTINGS)

        self.t0 = time.monotonic()
        self.state = State.SLEEPING
        self.state_since = self.t0

        # animated params, all eased toward targets each frame
        self.eye_open = 0.0        # 0 closed .. 1 open
        self.mouth_curve = 0.0     # -1 frown .. 1 smile
        self.eyebrow = 0.0         # -1 low .. 1 raised
        self.look_x = 0.0          # -1 left .. 1 right
        self.look_y = 0.0
        self.bob = 0.0
        self.sway = 0.0            # subtle horizontal head drift
        self.tilt = 0.0            # subtle head rotation, radians
        self.breath = 0.0
        # external gaze target (e.g. cursor position), blended in while idle
        self.gaze_target_x = 0.0
        self.gaze_target_y = 0.0
        # short celebratory expression fired on completed tasks
        self._happy_pulse_until = 0.0
        # live mic input level while listening (0..1), and a simple talking
        # pulse while TTS is speaking — see set_audio_level()/set_speaking()
        self.audio_level = 0.0
        self._speaking = False
        # wake-sequence reveal params (rainbow dot -> face forming)
        self.face_scale = 0.0
        self.left_eye_reveal = 0.0
        self.right_eye_reveal = 0.0
        self.mouth_reveal = 0.0

        self._blink_next = self._rand_blink_delay()
        self._last_blink_time = self.t0

    def _rand_blink_delay(self):
        import random
        return random.uniform(2.5, 5.5)

    def set_state(self, state):
        if state != self.state:
            self.state = state
            self.state_since = time.monotonic()

    def set_gaze_target(self, dx, dy):
        """dx/dy are -1..1, e.g. direction toward the mouse cursor. Only
        influences the face while genuinely idle (not thinking/waking/etc)."""
        self.gaze_target_x = max(-1.0, min(1.0, dx))
        self.gaze_target_y = max(-1.0, min(1.0, dy))

    def pulse_happy(self):
        """Brief celebratory expression — call when a task completes successfully."""
        self._happy_pulse_until = time.monotonic() + 0.7
        if self.anim["interaction_enabled"]:
            self.squash.kick(-5.0 * self.anim["interaction_intensity"])  # a little hop

    def tap(self):
        """A quick squash-and-recover when Toby is clicked."""
        if self.anim["interaction_enabled"]:
            self.squash.kick(7.0 * self.anim["interaction_intensity"])

    def react(self, kind, direction=0.0):
        """Glance at something that just happened on the desktop.

        Small on purpose: a look and a raised brow, over in half a second.
        It should be the kind of thing you only notice if you're looking.
        """
        if not (self.anim["desktop_reactions"] and self.state == State.IDLE):
            return
        strength = self.anim["idle_intensity"]
        if kind in ("workspace", "workspacev2"):
            self.glance.kick(direction * 9.0 * strength)
        elif kind in ("openwindow", "urgent"):
            self.brow_react.kick(4.0 * strength)
            self.glance.kick(2.5 * strength)
        elif kind == "closewindow":
            self.glance.kick(-2.5 * strength)
        elif kind == "fullscreen":
            self.brow_react.kick(3.0 * strength)

    def reload_animation_settings(self):
        self.anim = toby_anim.animation_settings(SETTINGS)

    def needs_full_frame_rate(self):
        """True while anything is changing fast enough to need every frame.

        Idle breathing and drifting are slow enough that a third of the
        frames look the same; blinks, reactions, state changes, thinking,
        listening and talking get the full rate.
        """
        now = time.monotonic()
        if self.state in (State.WAKING, State.THINKING, State.LISTENING, State.SPEAKING):
            return True
        if now - self.state_since < 1.0 or now < self._happy_pulse_until:
            return True
        if now - self._last_blink_time < 0.25:
            return True
        return not (self.squash.at_rest(0.01) and self.glance.at_rest(0.01)
                    and self.brow_react.at_rest(0.01))

    def set_audio_level(self, level):
        """0..1 mic input level, fed continuously while Voice Mode is listening —
        makes the face genuinely react to how loud you're talking, not a canned loop."""
        self.audio_level = max(0.0, min(1.0, level))

    def set_speaking(self, speaking):
        self._speaking = speaking

    def tick(self, mood="neutral"):
        now = time.monotonic()
        elapsed_in_state = now - self.state_since
        dt = now - self._last_tick_time
        self._last_tick_time = now
        self.squash.step(dt)
        self.glance.step(dt)
        self.brow_react.step(dt)

        target_eye = 1.0
        target_mouth = 0.3
        target_eyebrow = 0.0
        target_look_x = 0.0
        target_look_y = 0.0
        target_face_scale = 1.0
        target_left_eye_reveal = 1.0
        target_right_eye_reveal = 1.0
        target_mouth_reveal = 1.0

        if self.state == State.SLEEPING:
            target_eye = 0.0
            target_mouth = 0.0
            target_face_scale = 0.0
            target_left_eye_reveal = 0.0
            target_right_eye_reveal = 0.0
            target_mouth_reveal = 0.0
        elif self.state == State.WAKING:
            # staggered "rainbow dot -> chibi face forms" reveal
            target_mouth = 0.5
            target_face_scale = min(1.0, elapsed_in_state / 0.12)
            target_left_eye_reveal = max(0.0, min(1.0, (elapsed_in_state - 0.08) / 0.12))
            target_right_eye_reveal = max(0.0, min(1.0, (elapsed_in_state - 0.16) / 0.12))
            target_mouth_reveal = max(0.0, min(1.0, (elapsed_in_state - 0.24) / 0.1))
            target_eye = target_right_eye_reveal
        elif self.state == State.IDLE:
            # drift around lazily, but bias toward the cursor if it's nearby —
            # makes the face feel aware of the user rather than just looping
            drift_x = math.sin(now * 0.4) * 0.3
            drift_y = math.sin(now * 0.27) * 0.15
            target_look_x = lerp(drift_x, self.gaze_target_x, 0.5)
            target_look_y = lerp(drift_y, self.gaze_target_y, 0.5)
            target_mouth = 0.2
        elif self.state == State.LISTENING:
            target_look_y = -0.1
            # genuinely reacts to how loud you're talking, not a canned loop
            target_mouth = 0.3 + self.audio_level * 0.5
            target_eyebrow = 0.15 + self.audio_level * 0.3
        elif self.state == State.SPEAKING:
            # simple talking pulse timed to TTS playback — honestly not real
            # amplitude-driven lip-sync (we don't have per-phoneme timing from
            # espeak-ng), just an animated approximation while it's speaking
            target_mouth = 0.4 + 0.5 * abs(math.sin(now * 9.0))
            target_eyebrow = 0.15
        elif self.state == State.THINKING:
            target_look_y = -0.6
            target_look_x = math.sin(now * 2.0) * 0.4
            target_mouth = 0.0
            target_eyebrow = 0.3
        elif self.state == State.RESPONDING:
            tweak = MOOD_TO_STATE_TWEAK.get(mood, MOOD_TO_STATE_TWEAK["neutral"])
            target_mouth = tweak["mouth"]
            target_eyebrow = tweak["eyebrow"]

        # a completed-task pulse briefly overrides toward a bigger, brighter smile
        if now < self._happy_pulse_until:
            pulse_t = 1.0 - (self._happy_pulse_until - now) / 0.7  # 0..1 through the pulse
            pulse_strength = math.sin(pulse_t * math.pi)  # rises then falls
            target_mouth = max(target_mouth, 0.6 + 0.4 * pulse_strength)
            target_eyebrow = max(target_eyebrow, 0.3 * pulse_strength)

        # blink overrides eye target briefly, except while sleeping/waking
        if self.state not in (State.SLEEPING, State.WAKING):
            if now - self._last_blink_time > self._blink_next:
                self._last_blink_time = now
                self._blink_next = self._rand_blink_delay()
            since_blink = now - self._last_blink_time
            if since_blink < 0.12:
                target_eye = 1.0 - (since_blink / 0.12)

        rate = 0.4  # smoothing factor per tick — snappier response than a slow float
        self.eye_open = lerp(self.eye_open, target_eye, rate)
        self.mouth_curve = lerp(self.mouth_curve, target_mouth, rate)
        self.eyebrow = lerp(self.eyebrow, target_eyebrow, rate)
        self.look_x = lerp(self.look_x, target_look_x, rate)
        self.look_y = lerp(self.look_y, target_look_y, rate)
        self.face_scale = lerp(self.face_scale, target_face_scale, rate)
        self.left_eye_reveal = lerp(self.left_eye_reveal, target_left_eye_reveal, rate)
        self.right_eye_reveal = lerp(self.right_eye_reveal, target_right_eye_reveal, rate)
        self.mouth_reveal = lerp(self.mouth_reveal, target_mouth_reveal, rate)

        # idle life, scaled by the user's idle intensity (0 = perfectly still)
        idle = self.anim["idle_intensity"] if self.anim["idle_enabled"] else 0.0
        self.breath = math.sin(now * 1.6) * 1.5 * idle
        self.bob = math.sin(now * 1.1) * 2.0 * idle if self.state != State.SLEEPING else 0.0
        self.sway = (math.sin(now * 0.65) * 1.2 * idle
                     if self.state not in (State.SLEEPING, State.WAKING) else 0.0)
        self.tilt = (math.sin(now * 0.5) * 0.035 * idle if self.state == State.IDLE
                     else lerp(self.tilt, 0.0, 0.2))
        self.look_x = max(-1.2, min(1.2, self.look_x + self.glance.value * 0.08))
        self.eyebrow += self.brow_react.value * 0.06

        self.queue_draw()

    def on_draw(self, widget, cr):
        w, h = self.get_allocated_width(), self.get_allocated_height()
        cx, cy = w / 2 + self.sway, h / 2 + self.bob
        r = (self.FACE_DIAMETER / 2 - 3 + self.breath * 0.3) * max(0.06, self.face_scale)

        # squash and stretch about the bottom of the face, so a tap reads as
        # something soft being pressed rather than a picture being scaled
        sq = max(-0.22, min(0.22, self.squash.value * 0.05))
        if abs(sq) > 0.001:
            cr.translate(cx, cy + r)
            cr.scale(1 + sq * 0.7, 1 - sq)
            cr.translate(-cx, -(cy + r))

        if self.state == State.THINKING and r > 4:
            self._draw_thinking_motes(cr, cx, cy, r)

        # soft contact shadow beneath the face for a touch of depth
        if r > 1:
            shadow_grad = cairo.RadialGradient(cx, cy + r * 0.85, 0, cx, cy + r * 0.85, r * 0.9)
            shadow_grad.add_color_stop_rgba(0, 0, 0, 0, 0.28)
            shadow_grad.add_color_stop_rgba(1, 0, 0, 0, 0)
            cr.set_source(shadow_grad)
            cr.save()
            cr.translate(cx, cy + r * 0.85)
            cr.scale(1.0, 0.35)
            cr.arc(0, 0, r * 0.9, 0, 2 * math.pi)
            cr.restore()
            cr.fill()

        cr.save()
        cr.translate(cx, cy)
        cr.rotate(self.tilt)
        cr.translate(-cx, -cy)

        # face circle — soft gradient
        grad = cairo.RadialGradient(cx - r * 0.3, cy - r * 0.3, r * 0.1, cx, cy, r)
        grad.add_color_stop_rgba(0, 1, 0.85, 0.35, 1)
        grad.add_color_stop_rgba(1, 1, 0.65, 0.15, 1)
        cr.set_source(grad)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.fill()

        eye_r = r * 0.13
        eye_dx = r * 0.35
        eye_y = cy - r * 0.12 + self.look_y * r * 0.15
        for side in (-1, 1):
            reveal = self.left_eye_reveal if side == -1 else self.right_eye_reveal
            if reveal <= 0.01:
                continue
            ex = cx + side * eye_dx + self.look_x * r * 0.12
            open_amt = max(0.08, self.eye_open)
            cr.save()
            cr.translate(ex, eye_y)
            cr.scale(reveal, open_amt * reveal)
            cr.arc(0, 0, eye_r, 0, 2 * math.pi)
            cr.set_source_rgba(0.15, 0.1, 0.05, reveal)
            cr.fill()
            cr.restore()

            # eyebrow
            brow_y = eye_y - eye_r * 2.2 - self.eyebrow * eye_r
            cr.set_line_width(r * 0.05)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.move_to(ex - eye_r * 0.9, brow_y + self.eyebrow * eye_r * 0.3)
            cr.line_to(ex + eye_r * 0.9, brow_y - self.eyebrow * eye_r * 0.3)
            cr.set_source_rgba(0.15, 0.1, 0.05, 0.8 * reveal)
            cr.stroke()

        # mouth — quadratic curve, curvature follows mouth_curve
        mw = r * 0.55
        my = cy + r * 0.38
        curve = self.mouth_curve * r * 0.28
        cr.move_to(cx - mw / 2, my)
        cr.curve_to(cx - mw / 4, my + curve, cx + mw / 4, my + curve, cx + mw / 2, my)
        cr.set_line_width(r * 0.07)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_source_rgba(0.15, 0.1, 0.05, 0.85 * self.mouth_reveal)
        cr.stroke()

        cr.restore()
        return False

    def _draw_thinking_motes(self, cr, cx, cy, r):
        """Three soft motes drifting around the top of the head.

        In place of a spinner: slow, uneven and gentle, so it reads as Toby
        mulling something over rather than a progress bar in disguise.
        """
        t = time.monotonic() - self.state_since
        for i in range(3):
            phase = t * (0.9 + i * 0.17) + i * 2.1
            angle = -math.pi / 2 + math.sin(phase) * 1.15
            dist = r + 4.5 + math.sin(phase * 1.7) * 1.2
            mx = cx + math.cos(angle) * dist
            my = cy + math.sin(angle) * dist
            alpha = 0.35 + 0.35 * (0.5 + 0.5 * math.sin(phase * 2.3))
            hue = (t * 0.08 + i / 3.0) % 1.0
            rr, gg, bb = RingFlash._hsv_to_rgb(hue, 0.45, 1.0)
            cr.set_source_rgba(rr, gg, bb, alpha * min(1.0, t * 3))
            cr.arc(mx, my, 1.8 + 0.5 * math.sin(phase * 3), 0, 2 * math.pi)
            cr.fill()


# ---------------------------------------------------------------------------
# Rainbow ring flash — separate fullscreen transparent layer
# ---------------------------------------------------------------------------

class RingFlash(Gtk.Window):
    DURATION = 0.7  # snappier flash, synced to the faster wake sequence

    def __init__(self):
        super().__init__()
        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_namespace(self, "apple-agent-ring")
        for edge in (GtkLayerShell.Edge.TOP, GtkLayerShell.Edge.BOTTOM,
                     GtkLayerShell.Edge.LEFT, GtkLayerShell.Edge.RIGHT):
            GtkLayerShell.set_anchor(self, edge, True)
        GtkLayerShell.set_exclusive_zone(self, -1)
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)

        self.set_app_paintable(True)
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)

        self.drawing = Gtk.DrawingArea()
        self.drawing.connect("draw", self.on_draw)
        self.add(self.drawing)
        self.set_decorated(False)
        self.show_all()
        self.set_visible(False)

        self.start_time = None

    def fire(self):
        self.start_time = time.monotonic()
        self.set_visible(True)
        GLib.timeout_add(FPS_MS, self._tick)

    def _tick(self):
        if self.start_time is None:
            return False
        elapsed = time.monotonic() - self.start_time
        if elapsed > self.DURATION:
            self.set_visible(False)
            self.start_time = None
            return False
        self.drawing.queue_draw()
        return True

    def on_draw(self, widget, cr):
        if self.start_time is None:
            return False
        elapsed = time.monotonic() - self.start_time
        progress = elapsed / self.DURATION
        w, h = widget.get_allocated_width(), widget.get_allocated_height()

        alpha = 1.0
        if progress < 0.1:
            alpha = progress / 0.1
        elif progress > 0.55:
            alpha = max(0.0, 1.0 - (progress - 0.55) / 0.45)

        base_thickness = 22
        glow_layers = [
            (base_thickness * 2.2, 0.12),
            (base_thickness * 1.5, 0.22),
            (base_thickness * 1.0, 0.4),
            (base_thickness * 0.5, 0.9),
        ]
        hue_shift = progress * 360
        steps = 140
        for thickness, layer_alpha in glow_layers:
            for i in range(steps):
                t = i / steps
                hue = (hue_shift + t * 360) % 360
                r, g, b = self._hsv_to_rgb(hue / 360, 0.8, 1.0)
                cr.set_source_rgba(r, g, b, alpha * layer_alpha)
                cr.set_line_width(thickness)
                self._stroke_perimeter_segment(cr, w, h, t, 1.0 / steps)
        return False

    def _stroke_perimeter_segment(self, cr, w, h, t0, dt):
        perim = 2 * (w + h)
        p0 = t0 * perim
        p1 = (t0 + dt) * perim
        cr.move_to(*self._point_on_perimeter(p0, w, h))
        cr.line_to(*self._point_on_perimeter(p1, w, h))
        cr.stroke()

    def _point_on_perimeter(self, p, w, h):
        p = p % (2 * (w + h))
        if p < w:
            return (p, 0)
        p -= w
        if p < h:
            return (w, p)
        p -= h
        if p < w:
            return (w - p, h)
        p -= w
        return (0, h - p)

    @staticmethod
    def _hsv_to_rgb(h, s, v):
        i = int(h * 6)
        f = h * 6 - i
        p = v * (1 - s)
        q = v * (1 - f * s)
        t = v * (1 - (1 - f) * s)
        i %= 6
        return [(v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q)][i]

# ---------------------------------------------------------------------------
# Dynamic Island — a small top-anchored pill that appears while Toby is doing
# work that doesn't need the main panel open (thinking, reading the screen,
# waiting on a confirmation). Click it to bring the main panel to front.
# ---------------------------------------------------------------------------

class DynamicIsland(Gtk.Window):
    def __init__(self, on_click, on_expand):
        super().__init__()
        self._on_click = on_click
        self._on_expand = on_expand
        self._visible_target = False
        self._click_timeout_id = None
        self._last_press_time = None
        self._showing_card = False

        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_namespace(self, "apple-agent-island")
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.TOP, True)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.TOP, -60)
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)

        self.set_app_paintable(True)
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.set_decorated(False)

        box = Gtk.EventBox()
        box.get_style_context().add_class("apple-agent-island")
        box.get_style_context().add_class("apple-agent-surface")
        box.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        inner.set_border_width(10)
        box.add(inner)

        self._progress_total = 0
        self._progress_target = 0.0
        self._progress_shown = 0.0
        self.dot = Gtk.DrawingArea()
        self.dot.set_size_request(16, 16)
        self.dot.connect("draw", self._draw_dot)
        inner.pack_start(self.dot, False, False, 0)

        self.label = Gtk.Label(label="")
        self.label.get_style_context().add_class("apple-agent-island-label")
        inner.pack_start(self.label, False, False, 0)

        box.connect("button-press-event", self._on_button_press)

        # The island has two shapes. While Toby is working it is the compact
        # pill above: a pulsing dot and one line of status, not worth
        # interrupting anyone for. When it has something for the user it
        # becomes the card below, with the reply itself and buttons for what
        # to do about it — because a notification that can only be read is a
        # notification you have to go and act on somewhere else.
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.remove(inner)  # re-parent it into the column, not add it twice
        column.pack_start(inner, False, False, 0)

        self.card_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.card_box.set_border_width(12)
        self.card_box.set_no_show_all(True)

        self.card_body = Gtk.Label(label="")
        self.card_body.set_xalign(0)
        self.card_body.set_line_wrap(True)
        self.card_body.set_max_width_chars(52)
        self.card_body.get_style_context().add_class("apple-agent-island-card-body")
        self.card_box.pack_start(self.card_body, False, False, 0)

        self.card_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.card_box.pack_start(self.card_actions, False, False, 0)

        column.pack_start(self.card_box, False, False, 0)
        box.add(column)
        self.add(box)

        self.show_all()
        self.set_visible(False)
        self._pulse_t0 = time.monotonic()
        GLib.timeout_add(50, self._pulse_tick)

    DOUBLE_CLICK_MS = 400

    def _on_button_press(self, widget, event):
        # GDK's own double-click detection (_2BUTTON_PRESS) does not fire
        # reliably on this layer-shell setup, which is why double-clicking
        # the island to open the task view did nothing. Time the gap between
        # presses by hand instead — the same approach the face already uses.
        if event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        if self._showing_card:
            # the card's own buttons say what happens; a stray click on the
            # background around them shouldn't also do something
            return False
        now_ms = event.time
        previous = self._last_press_time
        self._last_press_time = now_ms
        if previous is not None and 0 <= now_ms - previous <= self.DOUBLE_CLICK_MS:
            if self._click_timeout_id:
                GLib.source_remove(self._click_timeout_id)
                self._click_timeout_id = None
            self._last_press_time = None
            self._on_expand()
            return False

        # Hold the single click briefly in case a second one follows.
        if self._click_timeout_id:
            GLib.source_remove(self._click_timeout_id)

        def fire_single():
            self._click_timeout_id = None
            self._on_click()
            return False

        self._click_timeout_id = GLib.timeout_add(self.DOUBLE_CLICK_MS, fire_single)
        return False

    def _draw_dot(self, widget, cr):
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        cx, cy = w / 2, h / 2
        r = min(w, h) / 2
        pulse = 0.6 + 0.4 * math.sin((time.monotonic() - self._pulse_t0) * 4.0)
        if self._progress_total:
            # a ring that fills as steps finish, with the pulsing dot inside
            fraction = self._progress_shown
            cr.set_line_width(2.2)
            cr.set_source_rgba(1, 1, 1, 0.14)
            cr.arc(cx, cy, r - 1.2, 0, 2 * math.pi)
            cr.stroke()
            if fraction > 0:
                cr.set_source_rgb(*ACCENT_RGB)
                cr.arc(cx, cy, r - 1.2, -math.pi / 2, -math.pi / 2 + 2 * math.pi * fraction)
                cr.stroke()
            cr.set_source_rgba(*ACCENT_RGB, pulse)
            cr.arc(cx, cy, r * 0.38, 0, 2 * math.pi)
            cr.fill()
            return False
        cr.set_source_rgba(*ACCENT_RGB, pulse)
        cr.arc(cx, cy, r * 0.62, 0, 2 * math.pi)
        cr.fill()
        return False

    def set_progress(self, done, total, current_label=""):
        """Show "2 of 4 · Click" and fill the ring. total 0 clears it."""
        self._progress_total = total
        self._progress_target = (done / total) if total else 0.0
        if total:
            text = f"{min(done + 1, total)} of {total}"
            if current_label:
                text += f"  ·  {current_label}"
            elif done >= total:
                text = "Done"
            self.set_status(text)

    def _pulse_tick(self):
        if self.get_visible():
            # ease the ring toward its target rather than jumping a step at a time
            self._progress_shown += (self._progress_target - self._progress_shown) * 0.25
            self.dot.queue_draw()
        return True

    def set_status(self, text):
        self.label.set_text(text)

    def show_card(self, headline, body, actions):
        """Show the island as a notification card.

        actions is a list of (label, callback). Each callback is run on the
        GTK thread and the card closes itself afterwards, unless the callback
        returns the string "keep".
        """
        self.set_status(headline)
        for child in self.card_actions.get_children():
            self.card_actions.remove(child)
        self.card_body.set_text(body)
        self.card_body.set_visible(bool(body))

        for label, callback in actions:
            button = Gtk.Button(label=label)
            button.get_style_context().add_class("apple-agent-panel-button")
            button.connect("clicked", self._make_card_handler(callback))
            self.card_actions.pack_start(button, False, False, 0)

        self.card_box.set_visible(True)
        self.card_box.show_all()
        self.card_body.set_visible(bool(body))
        self._showing_card = True
        self.show_island(headline)

    def _make_card_handler(self, callback):
        def on_clicked(_button):
            outcome = None
            try:
                outcome = callback()
            except Exception as e:
                print("ISLAND CARD ACTION ERROR:", e, flush=True)
            if outcome != "keep":
                self.hide_island()
        return on_clicked

    def hide_card(self):
        self._showing_card = False
        self.card_box.set_visible(False)
        for child in self.card_actions.get_children():
            self.card_actions.remove(child)

    def showing_card(self):
        return self._showing_card

    def show_island(self, text):
        self.set_status(text)
        if self._visible_target:
            return
        self._visible_target = True
        self.set_visible(True)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.TOP, -60)

        state = {"i": 0}
        steps = 10

        def step():
            if not self._visible_target:
                return False
            state["i"] += 1
            t = min(1.0, state["i"] / steps)
            margin = int(lerp(-60, 14, ease_out_cubic(t)))
            GtkLayerShell.set_margin(self, GtkLayerShell.Edge.TOP, margin)
            return t < 1.0

        GLib.timeout_add(14, step)

    def hide_island(self):
        self.hide_card()
        if not self._visible_target:
            return
        self._visible_target = False

        state = {"i": 0}
        steps = 10

        def step():
            if self._visible_target:
                return False
            state["i"] += 1
            t = min(1.0, state["i"] / steps)
            margin = int(lerp(14, -60, ease_out_cubic(t)))
            GtkLayerShell.set_margin(self, GtkLayerShell.Edge.TOP, margin)
            if t >= 1.0:
                self.set_visible(False)
                return False
            return True

        GLib.timeout_add(14, step)

# ---------------------------------------------------------------------------
# Island expanded view — double-click/hold the island to see this: the
# current task, a live step tracker, and a real Cancel button. No true
# "pause" — an in-flight LLM request can't meaningfully be paused, only
# cancelled — so only Cancel is offered rather than faking a pause.
# ---------------------------------------------------------------------------

class IslandExpanded(Gtk.Window):
    def __init__(self, on_cancel):
        super().__init__()
        self._on_cancel = on_cancel
        self._task_start = None

        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_namespace(self, "apple-agent-island-expanded")
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.TOP, True)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.TOP, 70)
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)

        self.set_app_paintable(True)
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.set_decorated(False)
        self.set_size_request(320, -1)

        outer = Gtk.EventBox()
        outer.get_style_context().add_class("apple-agent-island-expanded")
        outer.get_style_context().add_class("apple-agent-surface")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(16)
        outer.add(box)

        self.task_label = Gtk.Label(label="")
        self.task_label.set_xalign(0)
        self.task_label.get_style_context().add_class("apple-agent-island-task")
        box.pack_start(self.task_label, False, False, 0)

        self.elapsed_label = Gtk.Label(label="")
        self.elapsed_label.set_xalign(0)
        self.elapsed_label.get_style_context().add_class("apple-agent-island-elapsed")
        box.pack_start(self.elapsed_label, False, False, 0)

        self.steps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.pack_start(self.steps_box, False, False, 4)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.get_style_context().add_class("apple-agent-panel-button")
        cancel_btn.connect("clicked", lambda *_: self._on_cancel())
        box.pack_start(cancel_btn, False, False, 0)

        self.add(outer)
        self.show_all()
        self.set_visible(False)
        GLib.timeout_add(500, self._tick_elapsed)

    def _tick_elapsed(self):
        if self.get_visible() and self._task_start:
            secs = int(time.monotonic() - self._task_start)
            self.elapsed_label.set_text(f"{secs}s elapsed")
        return True

    def set_task(self, task_text, steps):
        """steps: list of (label, status) where status is 'done'/'current'/'pending'/'error'."""
        self.task_label.set_text(task_text)
        for child in self.steps_box.get_children():
            self.steps_box.remove(child)
        icons = {"done": "\u2713", "current": "\u25cf", "pending": "\u25cb", "error": "\u2715"}
        for label, status in steps:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            icon = Gtk.Label(label=icons.get(status, "\u25cb"))
            icon.get_style_context().add_class(f"apple-agent-step-{status}")
            row.pack_start(icon, False, False, 0)
            text = Gtk.Label(label=label)
            text.set_xalign(0)
            text.get_style_context().add_class(f"apple-agent-step-{status}")
            row.pack_start(text, False, False, 0)
            self.steps_box.pack_start(row, False, False, 0)
        self.steps_box.show_all()

    def open(self, task_text, steps):
        self._task_start = time.monotonic()
        self.set_task(task_text, steps)
        self.set_visible(True)

    def close(self):
        self._task_start = None
        self.set_visible(False)

# ---------------------------------------------------------------------------
# Study Helper — a small persistent panel that appears (top-right) whenever
# Study Mode is on, and disappears when it's off. Everything on it is a
# button the user clicks — summarizing, quizzing, and flashcards only ever
# happen because the user asked for them right now, never on a timer in the
# background. See the note atop study_notes.py for why that line matters.
# ---------------------------------------------------------------------------

HAND_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12),
    (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20),
    (0, 17),
]

CAMERA_ACTION_CHOICES = [
    ("toggle_panel", "Toggle Toby panel"),
    ("hide_panel", "Hide panel"),
    ("toggle_expanded", "Expand/collapse sidebar"),
    ("confirm_yes", "Confirm Yes"),
    ("confirm_no", "Confirm No"),
    ("workspace_prev", "Previous workspace"),
    ("workspace_next", "Next workspace"),
    ("toggle_fullscreen", "Toggle fullscreen"),
    ("cycle_nav", "Cycle sidebar section"),
    ("pulse_happy", "Happy face pulse"),
    ("expand_island", "Expand Dynamic Island"),
    ("close_island_expanded", "Close Dynamic Island detail"),
]


class CameraOverlay(Gtk.Window):
    """Bottom-left HUD for Camera Mode. Draws only derived landmark
    positions — the actual camera frame is never rendered, saved, or sent
    anywhere. Shows the last detected gesture and, while recording a custom
    gesture, a frame-count progress indicator."""

    def __init__(self):
        super().__init__()
        self.hands_xy = []      # list of list of (x, y) in 0..1, one list per detected hand
        self.gesture_flash = ""
        self.recording_label = ""

        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_namespace(self, "apple-agent-camera")
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.BOTTOM, True)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.LEFT, True)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.BOTTOM, 16)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.LEFT, 16)
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)

        self.set_app_paintable(True)
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.set_decorated(False)
        self.set_size_request(220, 220)

        outer = Gtk.EventBox()
        outer.get_style_context().add_class("apple-agent-island-expanded")
        outer.get_style_context().add_class("apple-agent-surface")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_border_width(10)
        outer.add(box)

        self.drawing = Gtk.DrawingArea()
        self.drawing.set_size_request(180, 160)
        self.drawing.connect("draw", self.on_draw)
        box.pack_start(self.drawing, True, True, 0)

        self.status_label = Gtk.Label(label="Camera Mode")
        self.status_label.set_xalign(0)
        self.status_label.get_style_context().add_class("apple-agent-island-elapsed")
        box.pack_start(self.status_label, False, False, 0)

        self.add(outer)
        self.show_all()
        self.set_visible(False)

    def set_hands(self, hands_xy):
        self.hands_xy = hands_xy
        self.drawing.queue_draw()

    def set_gesture_flash(self, name):
        self.gesture_flash = name
        self.status_label.set_text(name)
        self.drawing.queue_draw()

    def set_recording(self, label):
        self.recording_label = label
        self.status_label.set_text(label or "Camera Mode")

    def on_draw(self, widget, cr):
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        if not self.hands_xy:
            cr.set_source_rgba(1, 1, 1, 0.35)
            cr.select_font_face("sans-serif")
            cr.set_font_size(11)
            cr.move_to(8, h / 2)
            cr.show_text("No hand detected")
            return False
        for pts in self.hands_xy:
            screen_pts = [(x * w, y * h) for x, y in pts]
            cr.set_source_rgba(0.45, 0.85, 1.0, 0.8)
            cr.set_line_width(2)
            for a, b in HAND_CONNECTIONS:
                if a < len(screen_pts) and b < len(screen_pts):
                    cr.move_to(*screen_pts[a])
                    cr.line_to(*screen_pts[b])
                    cr.stroke()
            for x, y in screen_pts:
                cr.arc(x, y, 3, 0, 2 * math.pi)
                cr.set_source_rgba(1.0, 1.0, 1.0, 0.9)
                cr.fill()
        return False


class StudyHelper(Gtk.Window):
    def __init__(self, on_summarize, on_quiz, on_flashcards):
        super().__init__()
        self._on_summarize = on_summarize
        self._on_quiz = on_quiz
        self._on_flashcards = on_flashcards
        self._visible_target = False

        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_namespace(self, "apple-agent-study-helper")
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.TOP, True)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.RIGHT, True)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.TOP, 16)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.RIGHT, -260)
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)

        self.set_app_paintable(True)
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.set_decorated(False)
        self.set_size_request(220, -1)

        outer = Gtk.EventBox()
        outer.get_style_context().add_class("apple-agent-study-helper")
        outer.get_style_context().add_class("apple-agent-surface")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(14)
        outer.add(box)

        title = Gtk.Label(label="Study Mode")
        title.set_xalign(0)
        title.get_style_context().add_class("apple-agent-island-task")
        box.pack_start(title, False, False, 0)

        self.stats_label = Gtk.Label(label="")
        self.stats_label.set_xalign(0)
        self.stats_label.set_line_wrap(True)
        self.stats_label.get_style_context().add_class("apple-agent-island-elapsed")
        box.pack_start(self.stats_label, False, False, 4)

        summarize_btn = Gtk.Button(label="Summarize Screen")
        summarize_btn.get_style_context().add_class("apple-agent-panel-button")
        summarize_btn.connect("clicked", lambda *_: self._on_summarize())
        box.pack_start(summarize_btn, False, False, 0)

        quiz_btn = Gtk.Button(label="Quiz Me")
        quiz_btn.get_style_context().add_class("apple-agent-panel-button")
        quiz_btn.connect("clicked", lambda *_: self._on_quiz())
        box.pack_start(quiz_btn, False, False, 0)

        flashcards_btn = Gtk.Button(label="Flashcards")
        flashcards_btn.get_style_context().add_class("apple-agent-panel-button")
        flashcards_btn.connect("clicked", lambda *_: self._on_flashcards())
        box.pack_start(flashcards_btn, False, False, 0)

        self.status_label = Gtk.Label(label="")
        self.status_label.set_xalign(0)
        self.status_label.set_line_wrap(True)
        self.status_label.get_style_context().add_class("apple-agent-island-elapsed")
        box.pack_start(self.status_label, False, False, 4)

        self.add(outer)
        self.show_all()
        self.set_visible(False)

    def set_stats(self, text):
        self.stats_label.set_text(text)

    def set_status(self, text):
        self.status_label.set_text(text)

    def open_helper(self):
        if self._visible_target:
            return
        self._visible_target = True
        self.set_visible(True)
        state = {"i": 0}
        steps = 12

        def step():
            if not self._visible_target:
                return False
            state["i"] += 1
            t = min(1.0, state["i"] / steps)
            margin = int(lerp(-260, 16, ease_out_cubic(t)))
            GtkLayerShell.set_margin(self, GtkLayerShell.Edge.RIGHT, margin)
            return t < 1.0

        GLib.timeout_add(14, step)

    def close_helper(self):
        if not self._visible_target:
            return
        self._visible_target = False
        state = {"i": 0}
        steps = 10

        def step():
            if self._visible_target:
                return False
            state["i"] += 1
            t = min(1.0, state["i"] / steps)
            margin = int(lerp(16, -260, ease_out_cubic(t)))
            GtkLayerShell.set_margin(self, GtkLayerShell.Edge.RIGHT, margin)
            if t >= 1.0:
                self.set_visible(False)
                return False
            return True

        GLib.timeout_add(14, step)


# ---------------------------------------------------------------------------
# Quiz/flashcard review — one shared window for both, since the skeleton
# (progress counter, content area, next/reveal button) is the same either
# way. Built entirely from real saved study notes — never invented content.
# ---------------------------------------------------------------------------

class StudyReviewWindow(Gtk.Window):
    def __init__(self):
        super().__init__()
        self.mode = "quiz"  # or "flashcards"
        self.items = []
        self.index = 0
        self.score = 0
        self.revealed = False
        self.on_finish = None  # optional (subject, topic, score, total) -> None, set by caller per-session
        self.quiz_subject = None
        self.quiz_topic = None

        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_namespace(self, "apple-agent-study-review")
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.TOP, True)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.TOP, 90)
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.ON_DEMAND)

        self.set_app_paintable(True)
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.set_decorated(False)
        self.set_size_request(420, -1)

        outer = Gtk.EventBox()
        outer.get_style_context().add_class("apple-agent-island-expanded")
        outer.get_style_context().add_class("apple-agent-surface")
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.box.set_border_width(18)
        outer.add(self.box)

        self.progress_label = Gtk.Label(label="")
        self.progress_label.set_xalign(0)
        self.progress_label.get_style_context().add_class("apple-agent-island-elapsed")
        self.box.pack_start(self.progress_label, False, False, 0)

        self.content_label = Gtk.Label(label="")
        self.content_label.set_xalign(0)
        self.content_label.set_line_wrap(True)
        self.content_label.get_style_context().add_class("apple-agent-island-task")
        self.box.pack_start(self.content_label, False, False, 4)

        self.options_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.box.pack_start(self.options_box, False, False, 0)

        controls_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.reveal_btn = Gtk.Button(label="Reveal")
        self.reveal_btn.get_style_context().add_class("apple-agent-panel-button")
        self.reveal_btn.connect("clicked", self._on_reveal)
        controls_row.pack_start(self.reveal_btn, False, False, 0)
        self.next_btn = Gtk.Button(label="Next")
        self.next_btn.get_style_context().add_class("apple-agent-panel-button")
        self.next_btn.connect("clicked", self._on_next)
        controls_row.pack_start(self.next_btn, False, False, 0)
        close_btn = Gtk.Button(label="Close")
        close_btn.get_style_context().add_class("apple-agent-panel-button")
        close_btn.connect("clicked", lambda *_: self.close_review())
        controls_row.pack_start(close_btn, False, False, 0)
        self.box.pack_start(controls_row, False, False, 0)

        self.add(outer)
        self.show_all()
        self.set_visible(False)

    def open_review(self, mode, items, subject=None, topic=None):
        self.mode = mode
        self.items = items
        self.index = 0
        self.score = 0
        self.revealed = False
        self.quiz_subject = subject
        self.quiz_topic = topic
        if not items:
            self.progress_label.set_text("")
            self.content_label.set_text(
                "No study notes saved yet — summarize something or add a note first."
            )
            for child in self.options_box.get_children():
                self.options_box.remove(child)
            self.reveal_btn.set_visible(False)
            self.next_btn.set_visible(False)
        else:
            self.reveal_btn.set_visible(mode == "flashcards")
            self.next_btn.set_visible(True)
            self._render_current()
        self.set_visible(True)
        self.present()

    def close_review(self):
        self.set_visible(False)

    def _clear_options(self):
        for child in self.options_box.get_children():
            self.options_box.remove(child)

    def _render_current(self):
        self._clear_options()
        item = self.items[self.index]
        total = len(self.items)
        if self.mode == "quiz":
            self.progress_label.set_text(f"Question {self.index + 1}/{total} — score {self.score}")
            self.content_label.set_text(item.get("question", ""))
            for i, opt in enumerate(item.get("options", [])):
                btn = Gtk.Button(label=opt)
                btn.get_style_context().add_class("apple-agent-panel-button")
                btn.connect("clicked", lambda _b, idx=i: self._on_quiz_answer(idx))
                self.options_box.pack_start(btn, False, False, 0)
            self.options_box.show_all()
        else:  # flashcards
            self.revealed = False
            self.progress_label.set_text(f"Card {self.index + 1}/{total}")
            self.content_label.set_text(item.get("front", ""))
            self.reveal_btn.set_label("Reveal")

    def _on_quiz_answer(self, chosen_index):
        item = self.items[self.index]
        correct = item.get("answer_index", -1)
        for child in self.options_box.get_children():
            child.set_sensitive(False)
        if chosen_index == correct:
            self.score += 1
            self.content_label.set_text(item.get("question", "") + "\n\n\u2713 Correct!")
        else:
            correct_text = item.get("options", [])[correct] if 0 <= correct < len(item.get("options", [])) else "?"
            self.content_label.set_text(item.get("question", "") + f"\n\n\u2715 Not quite — the answer was: {correct_text}")

    def _on_reveal(self, *_a):
        if self.mode != "flashcards":
            return
        item = self.items[self.index]
        self.revealed = True
        self.content_label.set_text(item.get("front", "") + "\n\n\u2192 " + item.get("back", ""))
        self.reveal_btn.set_label("Revealed")

    def _on_next(self, *_a):
        if not self.items:
            return
        if self.index + 1 < len(self.items):
            self.index += 1
            self._render_current()
            for child in self.options_box.get_children():
                child.set_sensitive(True)
        else:
            if self.mode == "quiz":
                self.content_label.set_text(f"Done! Final score: {self.score}/{len(self.items)}")
                if self.on_finish and self.quiz_subject and self.quiz_topic and self.items:
                    self.on_finish(self.quiz_subject, self.quiz_topic, self.score, len(self.items))
            else:
                self.content_label.set_text("Done reviewing this set.")
            self._clear_options()
            self.next_btn.set_sensitive(False)


class TopicDetailWindow(Gtk.Window):
    """Opens when a topic card in the Study Plan grid is clicked. Shows real
    tracked proficiency (only ever moved by an actual quiz result — never
    self-reported or guessed), a way to quiz on it, and resource SEARCH
    links (not specific videos Toby can't verify exist)."""

    def __init__(self, on_quiz_topic):
        super().__init__()
        self._on_quiz_topic = on_quiz_topic
        self.current_topic = None

        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_namespace(self, "apple-agent-topic-detail")
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.TOP, True)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.TOP, 90)
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)

        self.set_app_paintable(True)
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.set_decorated(False)
        self.set_size_request(420, -1)

        outer = Gtk.EventBox()
        outer.get_style_context().add_class("apple-agent-island-expanded")
        outer.get_style_context().add_class("apple-agent-surface")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_border_width(18)
        outer.add(box)

        self.title_label = Gtk.Label(label="")
        self.title_label.set_xalign(0)
        self.title_label.get_style_context().add_class("apple-agent-island-task")
        box.pack_start(self.title_label, False, False, 0)

        self.proficiency_label = Gtk.Label(label="")
        self.proficiency_label.set_xalign(0)
        self.proficiency_label.get_style_context().add_class("apple-agent-island-elapsed")
        box.pack_start(self.proficiency_label, False, False, 0)

        quiz_btn = Gtk.Button(label="Quiz Me on This Topic")
        quiz_btn.get_style_context().add_class("apple-agent-panel-button")
        quiz_btn.connect("clicked", self._on_quiz_clicked)
        box.pack_start(quiz_btn, False, False, 4)

        links_title = Gtk.Label(label="Find resources (opens your browser):")
        links_title.set_xalign(0)
        links_title.get_style_context().add_class("apple-agent-dashboard-title")
        box.pack_start(links_title, False, False, 0)

        self.links_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.pack_start(self.links_box, False, False, 0)

        close_btn = Gtk.Button(label="Close")
        close_btn.get_style_context().add_class("apple-agent-panel-button")
        close_btn.connect("clicked", lambda *_: self.set_visible(False))
        box.pack_start(close_btn, False, False, 4)

        self.add(outer)
        self.show_all()
        self.set_visible(False)

    def open_topic(self, topic):
        self.current_topic = topic
        prof = topic.get("proficiency")
        prof_text = f"{prof}% proficient ({topic.get('quiz_count', 0)} quiz(zes) taken)" if prof is not None \
            else "Not assessed yet — take a quiz to find out where you stand"
        self.title_label.set_text(f"{topic['subject']} — {topic['topic']}")
        self.proficiency_label.set_text(prof_text)

        for child in self.links_box.get_children():
            self.links_box.remove(child)
        for link in study_plan.get_resource_links(topic["subject"], topic["topic"]):
            btn = Gtk.Button(label=link["label"])
            btn.get_style_context().add_class("apple-agent-panel-button")
            url = link["url"]
            btn.connect("clicked", lambda _b, u=url: subprocess.Popen(["xdg-open", u]))
            self.links_box.pack_start(btn, False, False, 0)
        self.links_box.show_all()

        self.set_visible(True)
        self.present()

    def _on_quiz_clicked(self, *_a):
        if self.current_topic:
            self._on_quiz_topic(self.current_topic["subject"], self.current_topic["topic"])


# ---------------------------------------------------------------------------
# Main assistant window
# ---------------------------------------------------------------------------
# The look: one small design system, not a pile of one-off styles
#
# Everything visual comes from the tokens below. There are no ad-hoc colours
# further down — a component picks a surface, an ink, a line and a radius
# from this list, and that is why the pill, the sidebar, the island and the
# study windows read as the same object rather than five things that happen
# to be dark.
#
# The palette is deep space: a near-black with a blue cast rather than a
# neutral grey, lifted one step at a time for each layer that sits closer to
# the user. Text is a faintly blue white at three fixed weights, so "quiet"
# always means the same thing in every panel. The rainbow belongs to the
# wake ring and the face, which are drawn in Cairo, not here; the interface
# around them stays out of their way.
#
# The sheet is generated rather than written out, because the accent colour
# is a user setting. Changing it rebuilds this sheet, which keeps the
# accented states at their designed contrast instead of layering a second,
# weaker rule on top of the first.
# ---------------------------------------------------------------------------

# -- surfaces, darkest first; each step is one layer closer to the user -----
SPACE_VOID = "#08090f"     # behind everything, and the shadow colour
SPACE_BASE = "#0b0c17"     # the main pill
SPACE_RAISED = "#0f1022"   # sidebar, island, study windows
SPACE_CONTROL = "#191c33"  # a control at rest
SPACE_CONTROL_HOVER = "#232746"
SPACE_CONTROL_ACTIVE = "#2c3157"

# -- ink, in three fixed weights --------------------------------------------
INK_BRIGHT = "#f4f5ff"                   # headings, values, anything being read
INK_NORMAL = "rgba(244, 245, 255, 0.78)"  # ordinary body text
INK_QUIET = "rgba(244, 245, 255, 0.48)"   # labels, hints, metadata

# -- hairlines ---------------------------------------------------------------
LINE_SOFT = "rgba(255, 255, 255, 0.06)"
LINE = "rgba(255, 255, 255, 0.11)"
LINE_STRONG = "rgba(255, 255, 255, 0.20)"

# -- meaning-carrying colours ------------------------------------------------
TONE_OK = "#6fe6a8"
TONE_WARN = "#f3c969"
TONE_ALERT = "#ff8080"

# -- type scale, in px -------------------------------------------------------
TYPE_MICRO = 11   # metadata, elapsed time, hints
TYPE_SMALL = 12   # labels, buttons
TYPE_BODY = 13    # body text
TYPE_LEAD = 15    # the input line and answers
TYPE_TITLE = 16   # section headings

# -- corner radii ------------------------------------------------------------
RADIUS_CONTROL = 10
RADIUS_CARD = 14
RADIUS_PANEL = 20
RADIUS_ISLAND = 26
RADIUS_PILL = 999

DEFAULT_ACCENT = "#5a8cff"


def _rgb(hex_color):
    """(r, g, b) from "#rrggbb", falling back to the default accent."""
    text = str(hex_color or "").lstrip("#")
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except (ValueError, IndexError):
        return _rgb(DEFAULT_ACCENT)


def _mix_with_white(hex_color, amount):
    """Lighten a colour toward white — used for an accent's hover state."""
    r, g, b = _rgb(hex_color)
    return (round(r + (255 - r) * amount),
            round(g + (255 - g) * amount),
            round(b + (255 - b) * amount))


def build_css(accent_hex=DEFAULT_ACCENT):
    """Render the whole stylesheet for one accent colour."""
    r, g, b = _rgb(accent_hex)
    lr, lg, lb = _mix_with_white(accent_hex, 0.25)
    accent = f"rgb({r}, {g}, {b})"
    accent_hover = f"rgb({lr}, {lg}, {lb})"
    accent_wash = f"rgba({r}, {g}, {b}, 0.18)"
    accent_edge = f"rgba({r}, {g}, {b}, 0.45)"
    accent_glow = f"rgba({r}, {g}, {b}, 0.40)"

    return f"""
/* ---------------------------------------------------------------------
 * Base: anything inside one of Toby's windows inherits these, so a plain
 * Gtk.Label or Gtk.Entry looks like it belongs here instead of falling
 * through to whatever the system theme happens to be. Component rules
 * below are deliberately placed after these and win on source order.
 * --------------------------------------------------------------------- */
.apple-agent-surface label {{
    color: {INK_NORMAL};
    font-size: {TYPE_BODY}px;
}}
.apple-agent-surface entry {{
    color: {INK_BRIGHT};
    background-color: {SPACE_CONTROL};
    background-image: none;
    border: 1px solid {LINE};
    border-radius: {RADIUS_CONTROL}px;
    padding: 7px 11px;
    font-size: {TYPE_BODY}px;
    caret-color: {INK_BRIGHT};
    box-shadow: none;
    transition: border 120ms ease-out, background-color 120ms ease-out;
}}
.apple-agent-surface entry:focus {{
    border: 1px solid {accent_edge};
    background-color: {SPACE_CONTROL_HOVER};
}}
.apple-agent-surface entry placeholder,
.apple-agent-surface entry:disabled {{
    color: {INK_QUIET};
}}
.apple-agent-surface button {{
    color: {INK_NORMAL};
    background-color: {SPACE_CONTROL};
    background-image: none;
    border: 1px solid {LINE};
    border-radius: {RADIUS_CONTROL}px;
    padding: 7px 14px;
    font-size: {TYPE_SMALL}px;
    text-shadow: none;
    box-shadow: none;
    transition: background-color 120ms ease-out, color 120ms ease-out, border 120ms ease-out;
}}
.apple-agent-surface button:hover {{
    color: {INK_BRIGHT};
    background-color: {SPACE_CONTROL_HOVER};
    border: 1px solid {LINE_STRONG};
}}
.apple-agent-surface button:active {{
    background-color: {SPACE_CONTROL_ACTIVE};
}}
.apple-agent-surface button:disabled {{
    color: {INK_QUIET};
    background-color: {SPACE_CONTROL};
    border: 1px solid {LINE_SOFT};
}}
.apple-agent-surface combobox button,
.apple-agent-surface combobox entry {{
    font-size: {TYPE_SMALL}px;
}}
.apple-agent-surface switch {{
    background-color: {SPACE_CONTROL};
    border: 1px solid {LINE};
    border-radius: {RADIUS_PILL}px;
}}
.apple-agent-surface switch:checked {{
    background-color: {accent};
    border: 1px solid {accent_edge};
}}
.apple-agent-surface switch slider {{
    background-color: {INK_BRIGHT};
    border-radius: {RADIUS_PILL}px;
}}
.apple-agent-surface scrolledwindow,
.apple-agent-surface flowbox,
.apple-agent-surface stack,
.apple-agent-surface box {{
    background-color: transparent;
}}
.apple-agent-surface scrollbar {{
    background-color: transparent;
    border: none;
}}
.apple-agent-surface scrollbar slider {{
    background-color: {LINE_STRONG};
    border: none;
    border-radius: {RADIUS_PILL}px;
    min-width: 6px;
    min-height: 28px;
}}
.apple-agent-surface scrollbar slider:hover {{
    background-color: {INK_QUIET};
}}

/* ---------------------------------------------------------------------
 * Surfaces
 * --------------------------------------------------------------------- */
.apple-agent-panel {{
    background-color: alpha({SPACE_BASE}, 0.82);
    border: 1px solid {LINE};
    border-radius: {RADIUS_PILL}px;
    padding: 2px 6px;
    /* A tight shadow on purpose. GTK 3 draws in software and re-blurs the
       shadow whenever anything inside the pill repaints, which is every
       frame the face moves; a 44px blur measured at ~18% of a CPU core
       with the pill idle, this one at a third of that. */
    box-shadow: 0 8px 16px alpha({SPACE_VOID}, 0.6),
                0 1px 0 rgba(255, 255, 255, 0.05) inset;
}}
.apple-agent-panel-hidden {{
    background-color: transparent;
    border-radius: {RADIUS_PILL}px;
    box-shadow: none;
}}
.apple-agent-sidebar-window {{
    background-color: alpha({SPACE_RAISED}, 0.985);
    border: 1px solid {LINE};
    border-radius: {RADIUS_PANEL}px;
    box-shadow: 0 24px 72px alpha({SPACE_VOID}, 0.78),
                0 1px 0 rgba(255, 255, 255, 0.05) inset;
}}
.apple-agent-island {{
    background-color: alpha({SPACE_RAISED}, 0.94);
    border: 1px solid {LINE};
    border-radius: {RADIUS_ISLAND}px;
    box-shadow: 0 6px 14px alpha({SPACE_VOID}, 0.6);   /* repaints while pulsing; kept tight */
}}
.apple-agent-island-expanded,
.apple-agent-study-helper {{
    background-color: alpha({SPACE_RAISED}, 0.97);
    border: 1px solid {LINE};
    border-radius: {RADIUS_PANEL}px;
    box-shadow: 0 18px 52px alpha({SPACE_VOID}, 0.7);
}}

/* ---------------------------------------------------------------------
 * The main pill
 * --------------------------------------------------------------------- */
.apple-agent-entry {{
    background-color: transparent;
    background-image: none;
    color: {INK_BRIGHT};
    border: none;
    border-radius: 0;
    padding: 6px 4px;
    font-size: {TYPE_LEAD}px;
    caret-color: {INK_BRIGHT};
    box-shadow: none;
    transition: box-shadow 150ms ease-out;
}}
.apple-agent-entry:focus {{
    outline: none;
    border: none;
    background-color: transparent;
    box-shadow: 0 2px 0 {accent_edge};
}}
.apple-agent-face-hit-target {{
    background-color: transparent;
    background-image: none;
    border: none;
    box-shadow: none;
}}
.apple-agent-close {{
    color: {INK_QUIET};
    background-color: transparent;
    background-image: none;
    border: none;
    border-radius: {RADIUS_PILL}px;
    padding: 5px 9px;
    min-width: 22px;
    min-height: 22px;
    font-size: {TYPE_SMALL}px;
    box-shadow: none;
    transition: background-color 120ms ease-out, color 120ms ease-out;
}}
.apple-agent-close:hover {{
    color: {INK_BRIGHT};
    background-color: rgba(255, 255, 255, 0.10);
    border: none;
}}
.apple-agent-close:active,
.apple-agent-close:checked {{
    color: {INK_BRIGHT};
    background-color: {accent_wash};
    border: none;
    box-shadow: 0 0 0 1px {accent_edge} inset;
}}
.apple-agent-answer {{
    color: {INK_BRIGHT};
    font-size: {TYPE_LEAD}px;
    padding: 2px 4px;
}}
.apple-agent-thinking {{
    color: {INK_QUIET};
    font-size: {TYPE_MICRO}px;
    font-family: monospace;
    padding: 0 4px;
}}

/* ---------------------------------------------------------------------
 * Shared small parts
 * --------------------------------------------------------------------- */
.apple-agent-badge {{
    color: {INK_BRIGHT};
    background-color: {accent_wash};
    border: 1px solid {accent_edge};
    border-radius: {RADIUS_PILL}px;
    padding: 3px 12px;
    font-size: {TYPE_SMALL}px;
}}
button.apple-agent-panel-button {{
    color: {INK_NORMAL};
    background-color: {SPACE_CONTROL};
    border: 1px solid {LINE};
    border-radius: {RADIUS_CONTROL}px;
    padding: 7px 14px;
    font-size: {TYPE_SMALL}px;
    transition: background-color 120ms ease-out, color 120ms ease-out, border 120ms ease-out;
}}
button.apple-agent-panel-button:hover {{
    color: {INK_BRIGHT};
    background-color: {SPACE_CONTROL_HOVER};
    border: 1px solid {LINE_STRONG};
}}
button.apple-agent-panel-button:active {{
    background-color: {SPACE_CONTROL_ACTIVE};
}}
button.apple-agent-primary-button {{
    color: {INK_BRIGHT};
    background-color: {accent};
    border: 1px solid {accent};
    border-radius: {RADIUS_CONTROL}px;
    padding: 8px 16px;
    font-size: {TYPE_SMALL}px;
    font-weight: bold;
}}
button.apple-agent-primary-button:hover {{
    background-color: {accent_hover};
    border: 1px solid {accent_hover};
}}

/* ---------------------------------------------------------------------
 * Dynamic Island
 * --------------------------------------------------------------------- */
.apple-agent-island-label {{
    color: {INK_BRIGHT};
    font-size: {TYPE_SMALL}px;
    font-weight: bold;
    padding: 0 2px;
}}
.apple-agent-island-card-body {{
    color: {INK_NORMAL};
    font-size: {TYPE_BODY}px;
    padding: 2px 0 4px 0;
}}
.apple-agent-island-task {{
    color: {INK_BRIGHT};
    font-size: {TYPE_TITLE}px;
    font-weight: bold;
}}
.apple-agent-island-elapsed {{
    color: {INK_QUIET};
    font-size: {TYPE_MICRO}px;
}}
.apple-agent-step-done {{
    color: {TONE_OK};
    font-size: {TYPE_SMALL}px;
}}
.apple-agent-step-current {{
    color: {INK_BRIGHT};
    font-size: {TYPE_SMALL}px;
    font-weight: bold;
}}
.apple-agent-step-pending {{
    color: {INK_QUIET};
    font-size: {TYPE_SMALL}px;
}}
.apple-agent-step-error {{
    color: {TONE_ALERT};
    font-size: {TYPE_SMALL}px;
}}

/* ---------------------------------------------------------------------
 * Sidebar navigation
 *
 * This is deliberately high contrast. An earlier version rendered the
 * selected item as a faint wash that was genuinely hard to see, so the
 * selected state here carries three cues at once: a filled accent
 * background, a brighter border, and bold text.
 * --------------------------------------------------------------------- */
.apple-agent-nav-sidebar {{
    background-color: alpha({SPACE_VOID}, 0.85);
    border-right: 1px solid {LINE};
}}
.apple-agent-nav-title {{
    color: {INK_QUIET};
    font-size: {TYPE_MICRO}px;
    font-weight: bold;
    letter-spacing: 1px;
    padding: 4px 8px;
}}
button.apple-agent-nav-button {{
    color: {INK_NORMAL};
    background-color: transparent;
    background-image: none;
    border: 1px solid transparent;
    border-radius: {RADIUS_CONTROL}px;
    padding: 11px 14px;
    font-size: {TYPE_BODY}px;
    transition: background-color 120ms ease-out, color 120ms ease-out;
}}
button.apple-agent-nav-button:hover {{
    color: {INK_BRIGHT};
    background-color: {SPACE_CONTROL};
    border: 1px solid {LINE};
}}
button.apple-agent-nav-button.nav-selected {{
    color: #ffffff;
    background-color: {accent};
    border: 1px solid {accent_hover};
    font-weight: bold;
    box-shadow: 0 2px 12px {accent_glow};
}}
button.apple-agent-nav-button.nav-selected:hover {{
    background-color: {accent_hover};
}}

/* ---------------------------------------------------------------------
 * Dashboard
 * --------------------------------------------------------------------- */
.apple-agent-section-heading {{
    color: {INK_BRIGHT};
    font-size: {TYPE_TITLE}px;
    font-weight: bold;
    padding: 2px 0;
}}
.apple-agent-dashboard-title {{
    color: {INK_QUIET};
    font-size: {TYPE_SMALL}px;
}}
.apple-agent-dashboard-value {{
    color: {INK_BRIGHT};
    font-size: {TYPE_BODY}px;
    font-weight: bold;
}}
.apple-agent-dashboard-row {{
    background-color: alpha({SPACE_CONTROL}, 0.55);
    border: 1px solid {LINE_SOFT};
    border-radius: {RADIUS_CONTROL}px;
    padding: 9px 12px;
}}

/* ---------------------------------------------------------------------
 * Chat transcript
 * --------------------------------------------------------------------- */
.apple-agent-history-user {{
    color: {INK_QUIET};
    font-size: {TYPE_BODY}px;
}}
.apple-agent-history-reply {{
    color: {INK_BRIGHT};
    font-size: {TYPE_BODY}px;
}}
.apple-agent-history-row {{
    background-color: alpha({SPACE_CONTROL}, 0.45);
    border: 1px solid {LINE_SOFT};
    border-left: 2px solid {accent_edge};
    border-radius: {RADIUS_CARD}px;
    padding: 11px 14px;
}}

/* ---------------------------------------------------------------------
 * Study Plan topic cards
 *
 * Colour carries the proficiency tier, so each tier also differs in border
 * weight — colour alone is not something to rely on.
 * --------------------------------------------------------------------- */
button.apple-agent-topic-card {{
    color: {INK_BRIGHT};
    font-size: {TYPE_SMALL}px;
    padding: 14px 12px;
    border-radius: {RADIUS_CARD}px;
    min-width: 150px;
    min-height: 74px;
}}
button.apple-agent-topic-none {{
    background-color: {SPACE_CONTROL};
    border: 1px solid {LINE};
}}
button.apple-agent-topic-none:hover {{
    background-color: {SPACE_CONTROL_HOVER};
}}
button.apple-agent-topic-low {{
    background-color: alpha({TONE_ALERT}, 0.22);
    border: 1px solid alpha({TONE_ALERT}, 0.55);
}}
button.apple-agent-topic-low:hover {{
    background-color: alpha({TONE_ALERT}, 0.32);
}}
button.apple-agent-topic-mid {{
    background-color: alpha({TONE_WARN}, 0.20);
    border: 1px solid alpha({TONE_WARN}, 0.55);
}}
button.apple-agent-topic-mid:hover {{
    background-color: alpha({TONE_WARN}, 0.30);
}}
button.apple-agent-topic-high {{
    background-color: alpha({TONE_OK}, 0.20);
    border: 1px solid alpha({TONE_OK}, 0.55);
}}
button.apple-agent-topic-high:hover {{
    background-color: alpha({TONE_OK}, 0.30);
}}
"""


CSS = build_css().encode()

# The Cairo-drawn widgets (waveform, memory graph) can't read the stylesheet,
# so the accent is mirrored here as plain floats and kept in step by
# apply_accent_color(). Without this they stayed a fixed blue while the rest
# of the interface followed the user's chosen colour.
ACCENT_RGB = tuple(c / 255.0 for c in _rgb(DEFAULT_ACCENT))


def set_accent_rgb(hex_color):
    global ACCENT_RGB
    ACCENT_RGB = tuple(c / 255.0 for c in _rgb(hex_color))



# ---------------------------------------------------------------------------
# Interactive memory graph — the Memories page. Pure Cairo, no matplotlib:
# pan by dragging, zoom with the scroll wheel, click a node to select it
# (the Memories page shows its text + a Delete button once selected).
# Layout (x/y per node) comes pre-computed from knowledge.get_graph_data().
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Waveform view — a small live level meter for Voice Mode, driven by real mic
# RMS values from voice_engine (not a decorative animation loop).
# ---------------------------------------------------------------------------

class FingerprintGlyph(Gtk.DrawingArea):
    """A drawn fingerprint mark for the permission prompt.

    Ridges are concentric arcs; while the reader is waiting, a highlight
    sweeps up through them, and it turns green on a match or briefly red on
    a miss. It animates only while a scan is running.
    """

    def __init__(self):
        super().__init__()
        self.set_size_request(26, 26)
        self.connect("draw", self.on_draw)
        self.state = "idle"        # idle | scanning | match | miss
        self._t0 = time.monotonic()
        self._timer = None

    def set_state(self, state):
        self.state = state
        self._t0 = time.monotonic()
        if state == "scanning" and self._timer is None:
            self._timer = GLib.timeout_add(33, self._tick)
        self.queue_draw()

    def _tick(self):
        if self.state != "scanning" or not self.get_visible():
            self._timer = None
            return False
        self.queue_draw()
        return True

    def on_draw(self, widget, cr):
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        cx, cy = w / 2, h / 2 + 2
        t = time.monotonic() - self._t0
        base = {"match": (0.435, 0.902, 0.659), "miss": (1.0, 0.5, 0.5)}.get(
            self.state, ACCENT_RGB)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_width(1.5)
        sweep = (t * 0.8) % 1.0
        for i in range(5):
            radius = 2.5 + i * 2.3
            level = i / 4.0
            glow = 1.0 - min(1.0, abs(level - sweep) * 3.0) if self.state == "scanning" else 0.0
            alpha = 0.45 + 0.55 * glow if self.state == "scanning" else 0.9
            cr.set_source_rgba(*base, alpha)
            cr.new_sub_path()
            cr.arc(cx, cy, radius, math.pi * (1.05 + i * 0.02), math.pi * (1.95 - i * 0.02))
            cr.stroke()
            cr.new_sub_path()
            cr.arc(cx, cy, radius, math.pi * 0.15, math.pi * (0.7 - i * 0.05))
            cr.stroke()
        return False


class WaveformView(Gtk.DrawingArea):
    BARS = 24

    def __init__(self):
        super().__init__()
        self.levels = [0.0] * self.BARS
        self.set_size_request(-1, 36)
        self.connect("draw", self.on_draw)

    def push_level(self, level):
        self.levels.append(max(0.0, min(1.0, level)))
        self.levels = self.levels[-self.BARS:]
        self.queue_draw()

    def on_draw(self, widget, cr):
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        bar_w = w / self.BARS
        accent_r, accent_g, accent_b = ACCENT_RGB
        for i, level in enumerate(self.levels):
            bar_h = max(2, level * h)
            x = i * bar_w
            cr.set_source_rgba(accent_r, accent_g, accent_b, 0.35 + 0.5 * level)
            cr.rectangle(x + 1, (h - bar_h) / 2, max(1, bar_w - 2), bar_h)
            cr.fill()
        return False


class MemoryGraphView(Gtk.DrawingArea):
    def __init__(self, on_select=None):
        super().__init__()
        self.nodes = []
        self.edges = []
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.zoom = 1.0
        self.selected_id = None
        self.on_select = on_select
        self._drag_start = None
        self._dragged = False

        self.set_can_focus(True)
        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.SCROLL_MASK
        )
        self.connect("draw", self.on_draw)
        self.connect("button-press-event", self.on_button_press)
        self.connect("button-release-event", self.on_button_release)
        self.connect("motion-notify-event", self.on_motion)
        self.connect("scroll-event", self.on_scroll)

    def load_data(self, nodes, edges):
        self.nodes = nodes
        self.edges = edges
        if self.selected_id and not any(n["id"] == self.selected_id for n in nodes):
            self.selected_id = None
        self.queue_draw()

    def reset_view(self):
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.zoom = 1.0
        self.queue_draw()

    def _node_screen_pos(self, node, w, h):
        margin = 50
        base_x = margin + node["x"] * max(1, (w - 2 * margin))
        base_y = margin + node["y"] * max(1, (h - 2 * margin))
        cx, cy = w / 2, h / 2
        sx = cx + (base_x - cx) * self.zoom + self.pan_x
        sy = cy + (base_y - cy) * self.zoom + self.pan_y
        return sx, sy

    @staticmethod
    def _category_color(category):
        # zlib.crc32, not hash(): Python randomizes string hashing per
        # process, so the whole graph came back in different colours after
        # every restart and a category never had a colour you could learn.
        hue = (zlib.crc32(str(category).encode("utf-8")) % 360) / 360.0
        i = int(hue * 6)
        f = hue * 6 - i
        s, v = 0.55, 0.85
        p = v * (1 - s)
        q = v * (1 - f * s)
        t = v * (1 - (1 - f) * s)
        i %= 6
        return [(v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q)][i]

    def on_draw(self, widget, cr):
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        if not self.nodes:
            cr.set_source_rgba(0.96, 0.96, 1.0, 0.48)
            cr.select_font_face("sans-serif")
            cr.set_font_size(13)
            cr.move_to(16, 26)
            cr.show_text("No memories yet — Toby adds facts here as you chat, or add one below.")
            return False

        pos_cache = {n["id"]: self._node_screen_pos(n, w, h) for n in self.nodes}

        cr.set_source_rgba(1, 1, 1, 0.14)
        cr.set_line_width(1)
        for e in self.edges:
            p1, p2 = pos_cache.get(e["source"]), pos_cache.get(e["target"])
            if p1 and p2:
                cr.move_to(*p1)
                cr.line_to(*p2)
                cr.stroke()

        for n in self.nodes:
            x, y = pos_cache[n["id"]]
            r = 7 * self.zoom
            cr_r, cr_g, cr_b = self._category_color(n["category"])
            is_sel = n["id"] == self.selected_id
            if is_sel:
                cr.set_source_rgba(*ACCENT_RGB, 1.0)
                cr.arc(x, y, r + 4, 0, 2 * math.pi)
                cr.set_line_width(2)
                cr.stroke()
            cr.set_source_rgba(cr_r, cr_g, cr_b, 0.92)
            cr.arc(x, y, r, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 0.85)
            cr.select_font_face("sans-serif")
            cr.set_font_size(max(8, 10 * self.zoom))
            label = n["label"]
            if len(label) > 26:
                label = label[:23] + "..."
            cr.move_to(x + r + 5, y + 4)
            cr.show_text(label)
        return False

    def on_button_press(self, widget, event):
        self.grab_focus()
        self._drag_start = (event.x, event.y, self.pan_x, self.pan_y)
        self._dragged = False
        return True

    def on_motion(self, widget, event):
        if self._drag_start:
            sx, sy, opx, opy = self._drag_start
            dx, dy = event.x - sx, event.y - sy
            if abs(dx) > 3 or abs(dy) > 3:
                self._dragged = True
            self.pan_x = opx + dx
            self.pan_y = opy + dy
            self.queue_draw()
        return True

    def on_button_release(self, widget, event):
        if not self._dragged:
            w, h = widget.get_allocated_width(), widget.get_allocated_height()
            hit = None
            for n in self.nodes:
                x, y = self._node_screen_pos(n, w, h)
                if (event.x - x) ** 2 + (event.y - y) ** 2 <= (12 * self.zoom) ** 2:
                    hit = n
                    break
            self.selected_id = hit["id"] if hit else None
            self.queue_draw()
            if self.on_select:
                self.on_select(hit)
        self._drag_start = None
        return True

    def on_scroll(self, widget, event):
        factor = 1.0
        if event.direction == Gdk.ScrollDirection.UP:
            factor = 1.1
        elif event.direction == Gdk.ScrollDirection.DOWN:
            factor = 1 / 1.1
        elif event.direction == Gdk.ScrollDirection.SMOOTH:
            factor = 1.0 - event.delta_y * 0.1
        self.zoom = max(0.3, min(3.0, self.zoom * factor))
        self.queue_draw()
        return True


class AssistantWindow(Gtk.Window):
    def __init__(self, ring: RingFlash):
        super().__init__()
        self.ring = ring
        self.history = load_history()
        self.current_mood = "neutral"
        self.expanded = False
        self._panel_generation = 0
        self._confirm_event = threading.Event()
        self._confirm_result = False

        self._hiding = False
        self._fingerprint_ready = False
        self._fingerprint_scan = None
        self.school_mode_config = school_mode.load_config()
        self.school_scheduled_active = False
        self._adaptive_check_running = False
        self._action_confirm_pending = False
        self._today_count = 0
        self._today_count_at = 0.0

        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_namespace(self, "apple-agent")
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.BOTTOM, True)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.BOTTOM, -300)
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)

        self.set_app_paintable(True)
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.set_decorated(False)
        self.set_default_size(1900, 60)
        self.set_size_request(1500, 50)

        self.css_provider = Gtk.CssProvider()
        self.css_provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            screen, self.css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        screen_control.get_screen_size = lambda: (screen.get_width(), screen.get_height())

        self.outer = Gtk.EventBox()
        self.outer.get_style_context().add_class("apple-agent-surface")
        self.outer.get_style_context().add_class("apple-agent-panel-hidden")
        self.add(self.outer)

        self.stage = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.stage.set_border_width(10)
        self.outer.add(self.stage)

        # -- input row: face + entry + close button -------------------------
        self.input_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.stage.pack_start(self.input_row, False, False, 0)

        self.face = Face()
        # Wrap the (visually small, 40px) face in a larger click target.
        # IMPORTANT: visible_window must be True — an EventBox with
        # visible_window=False has no GdkWindow of its own, and add_events()
        # silently does nothing on a windowless widget in GTK3. That was the
        # actual reason double-click never worked here at all.
        face_click_target = Gtk.EventBox()
        face_click_target.set_size_request(64, 64)
        face_click_target.set_visible_window(True)
        face_click_target.get_style_context().add_class("apple-agent-face-hit-target")
        face_click_target.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        face_click_target.connect("button-press-event", self.on_face_button_press)
        face_click_target.add(self.face)
        self.face.set_valign(Gtk.Align.CENTER)
        self.face.set_halign(Gtk.Align.CENTER)
        self.input_row.pack_start(face_click_target, False, False, 0)

        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("Ask me anything...")
        self.entry.get_style_context().add_class("apple-agent-entry")
        self.entry.set_no_show_all(True)  # revealed after wake animation
        self.entry.connect("activate", self.on_submit)
        self.entry.connect("changed", self.on_typing)
        self.entry.connect("key-press-event", self.on_key)
        self.input_row.pack_start(self.entry, True, True, 0)

        self.mic_btn = Gtk.Button(label="Mic")
        self.mic_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.mic_btn.get_style_context().add_class("apple-agent-close")
        self.mic_btn.set_tooltip_text("Tap: toggle Voice Mode. Hold: push-to-talk.")
        self.mic_btn.set_no_show_all(True)
        self.mic_btn.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK)
        self.mic_btn.connect("button-press-event", self.on_mic_button_press)
        self.mic_btn.connect("button-release-event", self.on_mic_button_release)
        self.input_row.pack_start(self.mic_btn, False, False, 0)

        self.camera_btn = Gtk.Button(label="Cam")
        self.camera_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.camera_btn.get_style_context().add_class("apple-agent-close")
        self.camera_btn.set_tooltip_text("Toggle Camera Mode (gestures)")
        self.camera_btn.set_no_show_all(True)
        self.camera_btn.connect("clicked", self.on_camera_button_clicked)
        self.input_row.pack_start(self.camera_btn, False, False, 0)

        self.expand_btn = Gtk.Button(label="\u25be")  # small down-chevron; guaranteed, non-gesture
        self.expand_btn.set_relief(Gtk.ReliefStyle.NONE)  # way to reach the sidebar (Memories/Settings/etc.)
        self.expand_btn.get_style_context().add_class("apple-agent-close")
        self.expand_btn.set_tooltip_text("Expand (Dashboard, Chat, Memories, Settings)")
        self.expand_btn.set_no_show_all(True)
        self.expand_btn.connect("clicked", lambda *_: self.toggle_expanded())
        self.input_row.pack_start(self.expand_btn, False, False, 0)

        self.smart_btn = Gtk.ToggleButton(label="Smart")
        self.smart_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.smart_btn.get_style_context().add_class("apple-agent-close")
        self.smart_btn.set_tooltip_text(
            "Route the next message(s) through your configured cloud API instead of "
            "the local model — for anything the local model struggles with. "
            "Requires an API key set in Settings. Stays on until you toggle it off."
        )
        self.smart_btn.set_no_show_all(True)
        self.smart_btn.connect("toggled", self.on_smart_toggled)
        self.input_row.pack_start(self.smart_btn, False, False, 0)

        self.close_btn = Gtk.Button(label="\u2715")
        self.close_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.close_btn.get_style_context().add_class("apple-agent-close")
        self.close_btn.connect("clicked", lambda *_: self.hide_panel())
        self.close_btn.set_no_show_all(True)
        self.input_row.pack_start(self.close_btn, False, False, 0)

        # -- voice status row: waveform + live transcript (Voice Mode only) --
        self.voice_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.voice_row.set_no_show_all(True)
        self.waveform = WaveformView()
        self.waveform.set_size_request(120, 28)
        self.voice_row.pack_start(self.waveform, False, False, 0)
        self.voice_transcript_label = Gtk.Label(label="")
        self.voice_transcript_label.set_xalign(0)
        self.voice_transcript_label.get_style_context().add_class("apple-agent-island-elapsed")
        self.voice_row.pack_start(self.voice_transcript_label, True, True, 0)
        self.stage.pack_start(self.voice_row, False, False, 0)
        self.voice_row.set_visible(False)

        # -- current exchange: streamed answer + inline confirm --------------
        self.current = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.stage.pack_start(self.current, False, True, 0)

        self.answer_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.current.pack_start(self.answer_row, False, True, 0)
        self.answer = Gtk.Label(label="")
        self.answer.set_line_wrap(True)
        self.answer.set_xalign(0)
        self.answer.get_style_context().add_class("apple-agent-answer")
        self.answer.set_no_show_all(True)
        self.answer_row.pack_start(self.answer, True, True, 0)

        self.thinking_content = Gtk.Label(label="")
        self.thinking_content.set_line_wrap(True)
        self.thinking_content.set_xalign(0)
        self.thinking_content.get_style_context().add_class("apple-agent-thinking")
        self.thinking_content.set_no_show_all(True)
        self.current.pack_start(self.thinking_content, False, True, 0)

        self.confirm_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.confirm_row.set_margin_start(48)
        self.confirm_row.set_no_show_all(True)
        self.current.pack_start(self.confirm_row, False, True, 0)
        self.fingerprint_glyph = FingerprintGlyph()
        self.fingerprint_glyph.set_no_show_all(True)
        self.confirm_row.pack_start(self.fingerprint_glyph, False, False, 0)
        confirm_label = Gtk.Label(label=self.CONFIRM_TEXT)
        self.confirm_label = confirm_label
        self.confirm_row.pack_start(confirm_label, False, False, 0)
        yes_btn = Gtk.Button(label="Yes")
        self.confirm_yes_btn = yes_btn
        yes_btn.connect("clicked", self.on_confirm_yes)
        self.confirm_row.pack_start(yes_btn, False, False, 0)
        no_btn = Gtk.Button(label="No")
        self.confirm_no_btn = no_btn
        no_btn.connect("clicked", self.on_confirm_no)
        self.confirm_row.pack_start(no_btn, False, False, 0)
        self.confirm_row.set_visible(False)

        # -- expanded content: sidebar navigation + stacked pages ------------
        # This lives in its OWN top-level layer-shell window now, not inside
        # the main pill. Every other secondary window (Dynamic Island detail,
        # quiz/flashcard review, topic detail) has been reliable; resizing the
        # small bottom pill to grow a sidebar inside it has not been — so this
        # reuses the pattern that's actually proven to work instead of
        # continuing to debug the one that hasn't.
        self.sidebar_window = Gtk.Window()
        GtkLayerShell.init_for_window(self.sidebar_window)
        GtkLayerShell.set_layer(self.sidebar_window, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_namespace(self.sidebar_window, "apple-agent-sidebar")
        GtkLayerShell.set_anchor(self.sidebar_window, GtkLayerShell.Edge.TOP, True)
        GtkLayerShell.set_margin(self.sidebar_window, GtkLayerShell.Edge.TOP, 80)
        GtkLayerShell.set_keyboard_mode(self.sidebar_window, GtkLayerShell.KeyboardMode.ON_DEMAND)
        self.sidebar_window.set_app_paintable(True)
        sidebar_visual = screen.get_rgba_visual()
        if sidebar_visual:
            self.sidebar_window.set_visual(sidebar_visual)
        self.sidebar_window.set_decorated(False)
        self.sidebar_window.set_size_request(1400, 720)

        # The background CSS class goes on an inner EventBox, not the
        # top-level Gtk.Window — every other window in this app (topic
        # detail, quiz review, island expanded) uses this same pattern, and
        # a plain app-paintable Gtk.Window doesn't reliably paint a CSS
        # background directly on itself, which is exactly why this was
        # rendering transparent.
        sidebar_outer = Gtk.EventBox()
        sidebar_outer.get_style_context().add_class("apple-agent-sidebar-window")
        sidebar_outer.get_style_context().add_class("apple-agent-surface")
        self.sidebar_window.add(sidebar_outer)

        self.expanded_area = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        sidebar_outer.add(self.expanded_area)

        self.nav_sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.nav_sidebar.set_size_request(186, -1)
        self.nav_sidebar.set_border_width(14)
        self.nav_sidebar.get_style_context().add_class("apple-agent-nav-sidebar")
        self.expanded_area.pack_start(self.nav_sidebar, False, False, 0)

        nav_title = Gtk.Label(label="MENU")
        nav_title.set_xalign(0)
        nav_title.get_style_context().add_class("apple-agent-nav-title")
        nav_title.set_margin_bottom(6)
        self.nav_sidebar.pack_start(nav_title, False, False, 0)

        self.content_stack = Gtk.Stack()
        self.content_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.content_stack.set_transition_duration(150)
        self.expanded_area.pack_start(self.content_stack, True, True, 0)

        self.nav_buttons = {}
        for key, nav_label in [("dashboard", "Dashboard"), ("chat", "Chat"),
                                ("memories", "Memories"), ("plan", "Study Plan"),
                                ("settings", "Settings")]:
            btn = Gtk.Button(label=nav_label)
            btn.set_relief(Gtk.ReliefStyle.NONE)
            btn.get_style_context().add_class("apple-agent-nav-button")
            btn.set_halign(Gtk.Align.FILL)
            btn.get_child().set_halign(Gtk.Align.START)
            btn.set_size_request(-1, 40)
            btn.connect("clicked", lambda _b, k=key: self.switch_nav(k))
            self.nav_sidebar.pack_start(btn, False, True, 0)
            self.nav_buttons[key] = btn

        # --- Dashboard page --------------------------------------------------
        dashboard_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        dashboard_page.set_border_width(24)
        dashboard_heading = Gtk.Label(label="Dashboard")
        dashboard_heading.set_xalign(0)
        dashboard_heading.get_style_context().add_class("apple-agent-section-heading")
        dashboard_heading.set_margin_bottom(6)
        dashboard_page.pack_start(dashboard_heading, False, False, 0)
        self.dashboard_labels = {}
        for stat_key, stat_title in [
            ("status", "AI Status"), ("task", "Current Task"), ("cpu", "CPU"),
            ("mem", "Memory"), ("context", "Context Window"), ("tools", "Active Tools"),
            ("automations", "Automations"), ("today", "Messages Today"),
        ]:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row.get_style_context().add_class("apple-agent-dashboard-row")
            title_lbl = Gtk.Label(label=stat_title)
            title_lbl.set_xalign(0)
            title_lbl.get_style_context().add_class("apple-agent-dashboard-title")
            title_lbl.set_size_request(160, -1)
            row.pack_start(title_lbl, False, False, 0)
            value_lbl = Gtk.Label(label="—")
            value_lbl.set_xalign(0)
            value_lbl.set_line_wrap(True)
            value_lbl.get_style_context().add_class("apple-agent-dashboard-value")
            row.pack_start(value_lbl, True, True, 0)
            dashboard_page.pack_start(row, False, False, 0)
            self.dashboard_labels[stat_key] = value_lbl
        self.content_stack.add_titled(dashboard_page, "dashboard", "Dashboard")

        # --- Chat page (conversation history) --------------------------------
        chat_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        chat_page.set_border_width(24)
        chat_heading = Gtk.Label(label="Chat")
        chat_heading.set_xalign(0)
        chat_heading.get_style_context().add_class("apple-agent-section-heading")
        chat_heading.set_margin_bottom(6)
        chat_page.pack_start(chat_heading, False, False, 0)
        self.history_scroller = Gtk.ScrolledWindow()
        self.history_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.history_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.history_scroller.add(self.history_box)
        chat_page.pack_start(self.history_scroller, True, True, 0)
        self.content_stack.add_titled(chat_page, "chat", "Chat")

        # --- Memories page (interactive graph) --------------------------------
        memories_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        memories_page.set_border_width(24)
        memories_heading = Gtk.Label(label="Memories")
        memories_heading.set_xalign(0)
        memories_heading.get_style_context().add_class("apple-agent-section-heading")
        memories_page.pack_start(memories_heading, False, False, 0)
        layout_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        layout_row.pack_start(Gtk.Label(label="Layout"), False, False, 0)
        self.memory_layout_buttons = {}
        current_layout = SETTINGS.get("memory_graph_layout", "force")
        for layout_key, layout_label in toby_settings.MEMORY_GRAPH_LAYOUTS:
            btn = Gtk.Button(label=layout_label)
            btn.get_style_context().add_class("apple-agent-nav-button")
            if layout_key == current_layout:
                btn.get_style_context().add_class("nav-selected")
            btn.connect("clicked", lambda _b, k=layout_key: self.on_memory_layout_clicked(k))
            layout_row.pack_start(btn, False, False, 0)
            self.memory_layout_buttons[layout_key] = btn
        memories_page.pack_start(layout_row, False, False, 0)

        self.memory_graph = MemoryGraphView(on_select=self.on_memory_node_select)
        self.memory_graph.set_size_request(-1, 380)
        memories_page.pack_start(self.memory_graph, True, True, 0)

        memory_hint = Gtk.Label(label="Drag to pan, scroll to zoom, click a node to select it.")
        memory_hint.set_xalign(0)
        memory_hint.get_style_context().add_class("apple-agent-dashboard-title")
        memories_page.pack_start(memory_hint, False, False, 0)

        memory_detail_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.memory_detail_label = Gtk.Label(label="Click a memory to see it here.")
        self.memory_detail_label.set_xalign(0)
        self.memory_detail_label.set_line_wrap(True)
        memory_detail_row.pack_start(self.memory_detail_label, True, True, 0)
        self.memory_delete_btn = Gtk.Button(label="Delete")
        self.memory_delete_btn.get_style_context().add_class("apple-agent-panel-button")
        self.memory_delete_btn.set_sensitive(False)
        self.memory_delete_btn.connect("clicked", self.on_memory_delete_clicked)
        memory_detail_row.pack_start(self.memory_delete_btn, False, False, 0)
        memories_page.pack_start(memory_detail_row, False, False, 0)
        self._selected_memory_id = None

        memory_add_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.memory_add_text = Gtk.Entry()
        self.memory_add_text.set_placeholder_text("New memory text…")
        memory_add_row.pack_start(self.memory_add_text, True, True, 0)
        self.memory_add_category = Gtk.Entry()
        self.memory_add_category.set_placeholder_text("category")
        self.memory_add_category.set_width_chars(12)
        memory_add_row.pack_start(self.memory_add_category, False, False, 0)
        memory_add_btn = Gtk.Button(label="Add")
        memory_add_btn.get_style_context().add_class("apple-agent-panel-button")
        memory_add_btn.connect("clicked", self.on_memory_add_clicked)
        memory_add_row.pack_start(memory_add_btn, False, False, 0)
        memories_page.pack_start(memory_add_row, False, False, 0)

        export_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        export_tree_btn = Gtk.Button(label="Export as Tree Image")
        export_tree_btn.get_style_context().add_class("apple-agent-panel-button")
        export_tree_btn.connect("clicked", lambda *_: self._export_graph_image(knowledge.render_tree))
        export_row.pack_start(export_tree_btn, False, False, 0)
        export_bubbles_btn = Gtk.Button(label="Export as Bubble Image")
        export_bubbles_btn.get_style_context().add_class("apple-agent-panel-button")
        export_bubbles_btn.connect("clicked", lambda *_: self._export_graph_image(knowledge.render_bubbles))
        export_row.pack_start(export_bubbles_btn, False, False, 0)
        memories_page.pack_start(export_row, False, False, 0)

        self.content_stack.add_titled(memories_page, "memories", "Memories")

        # --- Study Plan page (proficiency grid) ---------------------------------
        plan_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        plan_page.set_border_width(24)
        plan_heading = Gtk.Label(label="Study Plan")
        plan_heading.set_xalign(0)
        plan_heading.get_style_context().add_class("apple-agent-section-heading")
        plan_page.pack_start(plan_heading, False, False, 0)

        plan_hint = Gtk.Label(
            label="Proficiency only ever moves when you actually take a quiz on a topic — "
                  "it's never guessed. Click a topic for resources and to quiz on it."
        )
        plan_hint.set_xalign(0)
        plan_hint.set_line_wrap(True)
        plan_hint.get_style_context().add_class("apple-agent-dashboard-title")
        plan_page.pack_start(plan_hint, False, False, 0)

        upload_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        upload_btn = Gtk.Button(label="Upload Material (PDF/text)")
        upload_btn.get_style_context().add_class("apple-agent-panel-button")
        upload_btn.connect("clicked", self.on_upload_material_clicked)
        upload_row.pack_start(upload_btn, False, False, 0)
        self.upload_status_label = Gtk.Label(label="")
        self.upload_status_label.set_xalign(0)
        self.upload_status_label.get_style_context().add_class("apple-agent-island-elapsed")
        upload_row.pack_start(self.upload_status_label, True, True, 0)
        plan_page.pack_start(upload_row, False, False, 4)

        plan_scroller = Gtk.ScrolledWindow()
        plan_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.plan_grid = Gtk.FlowBox()
        self.plan_grid.set_valign(Gtk.Align.START)
        self.plan_grid.set_selection_mode(Gtk.SelectionMode.NONE)
        self.plan_grid.set_max_children_per_line(4)
        self.plan_grid.set_row_spacing(8)
        self.plan_grid.set_column_spacing(8)
        plan_scroller.add(self.plan_grid)
        plan_page.pack_start(plan_scroller, True, True, 0)

        self.content_stack.add_titled(plan_page, "plan", "Study Plan")

        # --- Settings page -----------------------------------------------------
        settings_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        settings_page.set_border_width(24)
        settings_heading = Gtk.Label(label="Settings")
        settings_heading.set_xalign(0)
        settings_heading.get_style_context().add_class("apple-agent-section-heading")
        settings_heading.set_margin_bottom(4)
        settings_page.pack_start(settings_heading, False, False, 0)

        style_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        style_row.pack_start(Gtk.Label(label="Response style"), False, False, 0)
        self.settings_style_combo = Gtk.ComboBoxText()
        for s in toby_settings.RESPONSE_STYLES:
            self.settings_style_combo.append(s, s.capitalize())
        self.settings_style_combo.set_active_id(SETTINGS.get("response_style", "balanced"))
        style_row.pack_start(self.settings_style_combo, False, False, 0)
        settings_page.pack_start(style_row, False, False, 0)

        memory_switch_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        memory_switch_row.pack_start(Gtk.Label(label="Remember facts from chat"), False, False, 0)
        self.settings_memory_switch = Gtk.Switch()
        self.settings_memory_switch.set_active(SETTINGS.get("memory_enabled", True))
        memory_switch_row.pack_end(self.settings_memory_switch, False, False, 0)
        settings_page.pack_start(memory_switch_row, False, False, 0)

        notif_switch_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        notif_switch_row.pack_start(Gtk.Label(label="Dynamic Island notifications"), False, False, 0)
        self.settings_notif_switch = Gtk.Switch()
        self.settings_notif_switch.set_active(SETTINGS.get("notifications_enabled", True))
        notif_switch_row.pack_end(self.settings_notif_switch, False, False, 0)
        settings_page.pack_start(notif_switch_row, False, False, 0)

        accent_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        accent_row.pack_start(Gtk.Label(label="Accent color (hex)"), False, False, 0)
        self.settings_accent_entry = Gtk.Entry()
        self.settings_accent_entry.set_text(SETTINGS.get("accent_color", "#5a8cff"))
        self.settings_accent_entry.set_width_chars(10)
        accent_row.pack_start(self.settings_accent_entry, False, False, 0)
        settings_page.pack_start(accent_row, False, False, 0)

        model_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        model_row.pack_start(Gtk.Label(label="Ollama model override"), False, False, 0)
        self.settings_model_entry = Gtk.Entry()
        self.settings_model_entry.set_placeholder_text("blank = the best one you have installed")
        self.settings_model_entry.set_text(SETTINGS.get("ollama_model", ""))
        model_row.pack_start(self.settings_model_entry, True, True, 0)
        settings_page.pack_start(model_row, False, False, 0)

        grade_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        grade_row.pack_start(Gtk.Label(label="Grade level (for study explanations)"), False, False, 0)
        self.settings_grade_combo = Gtk.ComboBoxText()
        for g in toby_settings.GRADE_LEVELS:
            self.settings_grade_combo.append(g, g)
        self.settings_grade_combo.set_active_id(SETTINGS.get("grade_level", "9th-10th"))
        grade_row.pack_start(self.settings_grade_combo, False, False, 0)
        settings_page.pack_start(grade_row, False, False, 0)

        voice_separator = Gtk.Label(label="Voice Mode")
        voice_separator.set_xalign(0)
        voice_separator.get_style_context().add_class("apple-agent-section-heading")
        settings_page.pack_start(voice_separator, False, False, 6)

        voice_switch_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        voice_switch_row.pack_start(Gtk.Label(label="Voice Mode enabled"), False, False, 0)
        self.settings_voice_switch = Gtk.Switch()
        self.settings_voice_switch.set_active(SETTINGS.get("voice_mode_enabled", False))
        voice_switch_row.pack_end(self.settings_voice_switch, False, False, 0)
        settings_page.pack_start(voice_switch_row, False, False, 0)

        wake_word_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        wake_word_row.pack_start(Gtk.Label(label=f'Require wake word ("{voice_engine.WAKE_WORD}")'), False, False, 0)
        self.settings_wake_word_switch = Gtk.Switch()
        self.settings_wake_word_switch.set_active(SETTINGS.get("voice_wake_word_enabled", True))
        wake_word_row.pack_end(self.settings_wake_word_switch, False, False, 0)
        settings_page.pack_start(wake_word_row, False, False, 0)

        voice_preset_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        voice_preset_row.pack_start(Gtk.Label(label="Voice"), False, False, 0)
        self.settings_voice_combo = Gtk.ComboBoxText()
        for v in voice_engine.VOICE_PRESETS:
            self.settings_voice_combo.append(v, v)
        self.settings_voice_combo.set_active_id(SETTINGS.get("voice_name", "en-us"))
        voice_preset_row.pack_start(self.settings_voice_combo, False, False, 0)
        settings_page.pack_start(voice_preset_row, False, False, 0)

        voice_rate_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        voice_rate_row.pack_start(Gtk.Label(label="Speech rate (wpm)"), False, False, 0)
        self.settings_rate_entry = Gtk.Entry()
        self.settings_rate_entry.set_text(str(SETTINGS.get("voice_rate", 165)))
        self.settings_rate_entry.set_width_chars(6)
        voice_rate_row.pack_start(self.settings_rate_entry, False, False, 0)
        voice_rate_row.pack_start(Gtk.Label(label="Pitch (0-99)"), False, False, 8)
        self.settings_pitch_entry = Gtk.Entry()
        self.settings_pitch_entry.set_text(str(SETTINGS.get("voice_pitch", 50)))
        self.settings_pitch_entry.set_width_chars(6)
        voice_rate_row.pack_start(self.settings_pitch_entry, False, False, 0)
        settings_page.pack_start(voice_rate_row, False, False, 0)

        voice_hint = Gtk.Label(
            label="Tap the mic button on the main bar to toggle Voice Mode any time; "
                  "hold it for push-to-talk regardless of this setting."
        )
        voice_hint.set_xalign(0)
        voice_hint.set_line_wrap(True)
        voice_hint.get_style_context().add_class("apple-agent-dashboard-title")
        settings_page.pack_start(voice_hint, False, False, 4)

        camera_separator = Gtk.Label(label="Camera Mode")
        camera_separator.set_xalign(0)
        camera_separator.get_style_context().add_class("apple-agent-section-heading")
        settings_page.pack_start(camera_separator, False, False, 6)

        camera_switch_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        camera_switch_row.pack_start(Gtk.Label(label="Camera Mode enabled"), False, False, 0)
        self.settings_camera_switch = Gtk.Switch()
        self.settings_camera_switch.set_active(SETTINGS.get("camera_mode_enabled", False))
        self.settings_camera_switch.connect("state-set", self._on_camera_switch_toggled)
        camera_switch_row.pack_end(self.settings_camera_switch, False, False, 0)
        settings_page.pack_start(camera_switch_row, False, False, 0)

        pinch_cursor_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        pinch_cursor_row.pack_start(Gtk.Label(label="Point with your hand to move the cursor"),
                                     False, False, 0)
        self.settings_pinch_cursor_switch = Gtk.Switch()
        self.settings_pinch_cursor_switch.set_active(SETTINGS.get("camera_pinch_cursor", False))
        self.settings_pinch_cursor_switch.connect("state-set", self._on_pinch_cursor_switch_toggled)
        pinch_cursor_row.pack_end(self.settings_pinch_cursor_switch, False, False, 0)
        settings_page.pack_start(pinch_cursor_row, False, False, 0)

        pinch_cursor_note = Gtk.Label(
            label="Your index fingertip moves the real cursor and pinching your thumb and "
                  "index finger together clicks, so you can select things without touching "
                  "the trackpad. Off by default, and the first time you turn it on it asks "
                  "for the same mouse-control permission any other pointer action needs. "
                  "Needs Camera Mode on as well."
        )
        pinch_cursor_note.set_xalign(0)
        pinch_cursor_note.set_line_wrap(True)
        pinch_cursor_note.get_style_context().add_class("apple-agent-dashboard-title")
        settings_page.pack_start(pinch_cursor_note, False, False, 4)

        camera_builtin_note = Gtk.Label(
            label="Built-in gestures: open palm = toggle panel, fist = hide, swipe = switch "
                  "workspace, push/pull = confirm/cancel, spread fingers = fullscreen, rotate = "
                  "cycle sidebar section, double-blink = confirm, head shake = cancel, eyebrows "
                  "raised = expand sidebar, smile = happy pulse. No video is ever displayed, "
                  "saved, or sent anywhere — only hand/face point positions are used."
        )
        camera_builtin_note.set_xalign(0)
        camera_builtin_note.set_line_wrap(True)
        camera_builtin_note.get_style_context().add_class("apple-agent-dashboard-title")
        settings_page.pack_start(camera_builtin_note, False, False, 4)

        self.camera_gesture_list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        settings_page.pack_start(self.camera_gesture_list_box, False, False, 0)

        camera_record_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.camera_gesture_name_entry = Gtk.Entry()
        self.camera_gesture_name_entry.set_placeholder_text("New gesture name…")
        camera_record_row.pack_start(self.camera_gesture_name_entry, True, True, 0)
        self.camera_gesture_action_combo = Gtk.ComboBoxText()
        for key, label in CAMERA_ACTION_CHOICES:
            self.camera_gesture_action_combo.append(key, label)
        camera_record_row.pack_start(self.camera_gesture_action_combo, False, False, 0)
        camera_record_btn = Gtk.Button(label="Record")
        camera_record_btn.get_style_context().add_class("apple-agent-panel-button")
        camera_record_btn.connect("clicked", self.on_camera_record_clicked)
        camera_record_row.pack_start(camera_record_btn, False, False, 0)
        settings_page.pack_start(camera_record_row, False, False, 0)

        camera_record_hint = Gtk.Label(
            label="Recording holds a ~1 second snapshot of your hand pose as the template — "
                  "works best for a distinct static pose (not a motion), since matching is "
                  "pose-based, not a trained model."
        )
        camera_record_hint.set_xalign(0)
        camera_record_hint.set_line_wrap(True)
        camera_record_hint.get_style_context().add_class("apple-agent-dashboard-title")
        settings_page.pack_start(camera_record_hint, False, False, 4)

        self._build_animation_settings(settings_page)

        fp_separator = Gtk.Label(label="Fingerprint")
        fp_separator.set_xalign(0)
        fp_separator.get_style_context().add_class("apple-agent-section-heading")
        settings_page.pack_start(fp_separator, False, False, 6)
        fp_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        fp_row.pack_start(Gtk.Label(label="Approve permission prompts with your fingerprint"),
                          False, False, 0)
        self.settings_fingerprint_switch = Gtk.Switch()
        self.settings_fingerprint_switch.set_active(SETTINGS.get("confirm_with_fingerprint", False))
        fp_row.pack_end(self.settings_fingerprint_switch, False, False, 0)
        settings_page.pack_start(fp_row, False, False, 0)
        fp_note = Gtk.Label(
            label="Uses fprintd, the standard Linux fingerprint service. Toby only hears "
                  "\"matched\" or \"didn't\" — nothing about your fingerprint itself. The Yes "
                  "and No buttons keep working. Enrol a finger first with: fprintd-enroll")
        fp_note.set_xalign(0)
        fp_note.set_line_wrap(True)
        fp_note.get_style_context().add_class("apple-agent-dashboard-title")
        settings_page.pack_start(fp_note, False, False, 4)

        cloud_separator = Gtk.Label(label="Cloud AI (optional)")
        cloud_separator.set_xalign(0)
        cloud_separator.get_style_context().add_class("apple-agent-section-heading")
        settings_page.pack_start(cloud_separator, False, False, 6)

        cloud_note = Gtk.Label(
            label="Off by default. Local Ollama handles everything normally — this is only "
                  "for the 'Smart' button on the main bar, for requests the local model "
                  "struggles with. Your key is stored only in settings.json on this machine "
                  "and sent only to OpenAI directly — never to Anthropic or anywhere else. "
                  "You pay OpenAI directly for whatever you use; this app does not track or "
                  "cap usage. Saving a key here is all that's needed — tap Smart on the main "
                  "bar whenever you want to actually use it for a message."
        )
        cloud_note.set_xalign(0)
        cloud_note.set_line_wrap(True)
        cloud_note.get_style_context().add_class("apple-agent-dashboard-title")
        settings_page.pack_start(cloud_note, False, False, 4)

        cloud_key_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        cloud_key_row.pack_start(Gtk.Label(label="OpenAI API key"), False, False, 0)
        self.settings_cloud_key_entry = Gtk.Entry()
        self.settings_cloud_key_entry.set_visibility(False)  # mask it — this is a live credential
        self.settings_cloud_key_entry.set_placeholder_text("sk-...")
        self.settings_cloud_key_entry.set_text(SETTINGS.get("cloud_api_key", ""))
        self.settings_cloud_key_entry.set_hexpand(True)
        cloud_key_row.pack_start(self.settings_cloud_key_entry, True, True, 0)
        settings_page.pack_start(cloud_key_row, False, False, 0)

        cloud_model_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        cloud_model_row.pack_start(Gtk.Label(label="Model"), False, False, 0)
        self.settings_cloud_model_entry = Gtk.Entry()
        self.settings_cloud_model_entry.set_text(SETTINGS.get("cloud_model", "gpt-4o"))
        cloud_model_row.pack_start(self.settings_cloud_model_entry, False, False, 0)
        settings_page.pack_start(cloud_model_row, False, False, 0)

        voice_auto_cloud_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        voice_auto_cloud_row.pack_start(
            Gtk.Label(label="Use cloud automatically for voice requests"), False, False, 0
        )
        self.settings_voice_auto_cloud_switch = Gtk.Switch()
        self.settings_voice_auto_cloud_switch.set_active(SETTINGS.get("voice_auto_cloud", False))
        voice_auto_cloud_row.pack_end(self.settings_voice_auto_cloud_switch, False, False, 0)
        settings_page.pack_start(voice_auto_cloud_row, False, False, 0)

        voice_auto_cloud_hint = Gtk.Label(
            label="Only applies to voice — typed messages still default to local unless you tap "
                  "Smart yourself. Needs a saved key above. This is the fast+smart combo for "
                  "hands-free use, at the cost of a cloud request every time you talk to Toby."
        )
        voice_auto_cloud_hint.set_xalign(0)
        voice_auto_cloud_hint.set_line_wrap(True)
        voice_auto_cloud_hint.get_style_context().add_class("apple-agent-dashboard-title")
        settings_page.pack_start(voice_auto_cloud_hint, False, False, 4)

        apps_note = Gtk.Label(
            label=f"{len(ALLOWED_APPS)} allowed apps, {len(ALLOWED_PACKAGES)} allowed packages — "
                  f"edit ALLOWED_APPS/ALLOWED_PACKAGES near the top of linux_agent_apple.py to change these."
        )
        apps_note.set_xalign(0)
        apps_note.set_line_wrap(True)
        apps_note.get_style_context().add_class("apple-agent-dashboard-title")
        settings_page.pack_start(apps_note, False, False, 4)

        self.settings_save_status = Gtk.Label(label="")
        self.settings_save_status.set_xalign(0)
        settings_page.pack_start(self.settings_save_status, False, False, 0)

        settings_save_btn = Gtk.Button(label="Save Settings")
        settings_save_btn.get_style_context().add_class("apple-agent-primary-button")
        settings_save_btn.set_halign(Gtk.Align.START)
        settings_save_btn.connect("clicked", self.on_settings_save_clicked)
        settings_page.pack_start(settings_save_btn, False, False, 0)

        settings_scroller = Gtk.ScrolledWindow()
        settings_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        settings_scroller.add(settings_page)
        self.content_stack.add_titled(settings_scroller, "settings", "Settings")

        self.content_stack.set_visible_child_name("chat")
        self.nav_buttons["chat"].get_style_context().add_class("nav-selected")
        study_mode_state.badge = None  # Study Mode status now lives in the Dashboard, not a standalone widget

        self.sidebar_window.show_all()
        self.sidebar_window.set_visible(False)

        self.connect("destroy", Gtk.main_quit)
        self.show_all()
        self.entry.set_visible(False)
        self.close_btn.set_visible(False)
        self.mic_btn.set_visible(False)
        self.camera_btn.set_visible(False)
        self.expand_btn.set_visible(False)
        self.smart_btn.set_visible(False)
        self.voice_row.set_visible(False)
        self.answer.set_visible(False)
        self.thinking_content.set_visible(False)
        self.confirm_row.set_visible(False)
        self.set_visible(False)

        self.last_interaction = time.monotonic()
        self.island = DynamicIsland(on_click=self.on_island_click, on_expand=self.on_island_expand)
        self.island_expanded = IslandExpanded(on_cancel=self.on_task_cancel)
        self.study_helper = StudyHelper(
            on_summarize=self.on_study_summarize,
            on_quiz=self.on_study_quiz,
            on_flashcards=self.on_study_flashcards,
        )
        self.study_review = StudyReviewWindow()
        self.study_review.on_finish = self.on_quiz_finished
        self.topic_detail = TopicDetailWindow(on_quiz_topic=self.on_topic_quiz_requested)
        study_mode_state.helper = self.study_helper
        if study_mode_state.active:
            self.study_helper.open_helper()

        self.voice = voice_engine.VoiceEngine(
            on_partial=self.on_voice_partial,
            on_final=self.on_voice_final,
            on_level=self.on_voice_level,
            on_state=self.on_voice_state,
        )
        self.voice.wake_word_enabled = SETTINGS.get("voice_wake_word_enabled", True)
        self.voice.voice = SETTINGS.get("voice_name", "en-us")
        self.voice.rate = SETTINGS.get("voice_rate", 165)
        self.voice.pitch = SETTINGS.get("voice_pitch", 50)
        self._mic_hold_timeout_id = None
        self._mic_holding = False
        self._voice_spoken_len = 0  # how much of the streaming reply we've already spoken aloud
        self.smart_mode_active = False
        if SETTINGS.get("voice_mode_enabled", False):
            self.voice.start()
            self.voice_row.set_visible(True)

        self.camera = camera_engine.CameraEngine(
            on_hand_frame=self.on_camera_hand_frame,
            on_face_frame=None,  # face landmarks drive gestures directly inside the engine; no UI needs the raw points
            on_gesture=self.on_camera_gesture,
            on_state=self.on_camera_state,
        )
        # React to the desktop: a glance toward the workspace you switched
        # to, a raised brow when a window opens. Read-only, and it reconnects
        # by itself if Hyprland restarts.
        self._last_workspace = None
        self.hypr_listener = hypr_events.HyprEventListener(
            lambda name, data: GLib.idle_add(self._on_desktop_event, name, data))
        self.hypr_listener.start()
        if SETTINGS.get("confirm_with_fingerprint"):
            self._check_fingerprint_reader()

        self.chibi_director = chibi.ChibiDirector(toby_anim.animation_settings(SETTINGS))
        ChibiStage = chibi.make_stage_class()
        self.chibi_stage = ChibiStage(self.chibi_director, accent_getter=lambda: ACCENT_RGB)

        self.camera_overlay = CameraOverlay()
        self._camera_recording_name = None
        self._pending_camera_action_for_record = None
        self._pinch_cursor_armed = False
        self._pinch_held = False
        self._pointer_smoothed = None
        self._last_pinch_click = 0.0
        self._pending_confirm_purpose = None
        self.refresh_camera_gesture_list()
        if SETTINGS.get("camera_mode_enabled", False):
            self.camera.start()
            self.camera_overlay.set_visible(True)
            if SETTINGS.get("camera_pinch_cursor", False):
                # Deliberately not armed yet — hand pointing still has to be
                # confirmed, and that prompt belongs on screen after the
                # window exists, not during construction.
                GLib.timeout_add_seconds(1, lambda: self._arm_pinch_cursor() or False)

        self.task_cancelled = False
        self.task_steps = []       # [(label, status), ...] for the expanded view
        self.task_label_text = ""
        self._request_id = 0
        self._waiting_for_first_chunk = False
        GLib.timeout_add(FPS_MS, self.tick)
        GLib.timeout_add(60000, self.check_school_schedule)
        GLib.timeout_add(300000, self.check_school_adaptive)
        GLib.timeout_add_seconds(2, self.refresh_dashboard)
        GLib.timeout_add_seconds(2, self.refresh_study_helper_stats)
        saved_accent = SETTINGS.get("accent_color", "")
        if len(saved_accent) == 7 and saved_accent.startswith("#"):
            self.apply_accent_color(saved_accent)
        # last, so everything it reports on already exists
        self._start_remote_bridge()
        threading.Thread(target=self._desktop_setup, daemon=True).start()
        if SETTINGS.get("startup_greeting", True):
            GLib.timeout_add(900, self._startup_greeting)

    def _desktop_setup(self):
        """Runtime-only Hyprland additions: the summon key and, if you turned
        them on, Toby's window animations. Nothing is written to your config;
        a Hyprland reload removes both, and they're re-added next start."""
        combo = SETTINGS.get("summon_keybind", "SUPER, G")
        if combo:
            status = hypr_keybind.ensure(combo)
            if status.startswith("taken"):
                print(f"KEYBIND: {combo} is already {status}; summon Toby with `toby` instead", flush=True)
        anim = toby_anim.animation_settings(SETTINGS)
        if anim["hypr_animations_enabled"]:
            hypr_animations.apply(anim["hypr_animation_speed"])

    def _startup_greeting(self):
        """Pop up briefly at login, say hello, and tuck away again.

        Once per login: a marker in the runtime directory (cleared when you
        log out) stops a restart from greeting you again.
        """
        if self.get_visible():
            return False
        marker = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "little-toby-greeted"
        if marker.exists():
            return False
        try:
            marker.touch()
        except OSError:
            pass
        self.show_panel()
        self.answer.set_text("Hi, I'm here. Press Super+G whenever you need me.")
        self.answer.set_visible(True)
        self.answer.show()
        GLib.timeout_add(3200, lambda: (self.hide_panel() if not self._busy
                                        and not self.entry.get_text() else None) and False)
        return False

    def _on_desktop_event(self, name, data):
        direction = 0.0
        if name in ("workspace", "workspacev2"):
            ident = data.split(",")[0]
            try:
                number = int(ident)
                if self._last_workspace is not None:
                    direction = 1.0 if number > self._last_workspace else -1.0
                self._last_workspace = number
            except ValueError:
                direction = 1.0
        self.face.react(name, direction)
        return False

    # -- the phone app ---------------------------------------------------------------
    def _start_remote_bridge(self):
        """Serve the phone app, if you've turned it on (`toby phone on`)."""
        self.remote = None
        self._busy = False
        self._remote_reply = ""
        self._last_remote_publish = 0.0
        if not SETTINGS.get("remote_enabled"):
            return
        window = self

        class Callbacks:
            # These run on the bridge's request threads, so they only read
            # simple flags and hand real work to the GTK thread.
            def ask(self, text):
                if window._busy:
                    return False, "Toby is still working on the last thing. Try again in a moment."
                window._busy = True
                GLib.idle_add(window._remote_ask, text)
                return True, "Sent."

            def confirm(self, answer):
                if not window.confirm_row.get_visible():
                    return False
                GLib.idle_add(lambda: (window.on_confirm_yes() if answer else window.on_confirm_no()) or False)
                return True

            def cancel(self):
                if not window._busy:
                    return False
                GLib.idle_add(lambda: window.on_task_cancel() or False)
                return True

        try:
            token = remote_bridge.load_token()
            self.remote = remote_bridge.RemoteBridge(
                Callbacks(), token, host=SETTINGS.get("remote_bind", "127.0.0.1"),
                port=int(SETTINGS.get("remote_port", 8765))).start()
            self._publish_remote_state(force=True)
        except OSError as e:
            print(f"PHONE BRIDGE: couldn't start ({e}); the phone app won't connect", flush=True)
            self.remote = None

    def _remote_ask(self, text):
        self.entry.set_text(text)
        self.on_submit(self.entry)
        return False

    def _publish_remote_state(self, force=False):
        """Tell a connected phone what Toby is doing right now."""
        remote = getattr(self, "remote", None)
        if remote is None:
            return False
        now = time.monotonic()
        if not force and now - self._last_remote_publish < 0.2:
            return False   # streaming text arrives fast; a few updates a second is plenty
        self._last_remote_publish = now
        steps = [{"label": label, "status": status}
                 for label, status in self.task_steps if label != "Thinking"]
        history = [{"role": m["role"], "content": m["content"][:400]} for m in self.history[-6:]]
        confirm = None
        if self.confirm_row.get_visible():
            confirm = {"pending": True, "text": self.confirm_label.get_text()}
        remote.publish({
            "busy": self._busy,
            "task": self._chibi_title() if self._busy or steps else "",
            "steps": steps,
            "reply": self._remote_reply,
            "confirm": confirm,
            "history": history,
        })
        return False

    def on_island_click(self):
        self.show_panel()

    def on_island_expand(self):
        self.island_expanded.open(self.task_label_text or "Idle", self.task_steps)

    def on_task_cancel(self):
        self.task_cancelled = True
        self._set_task_step_status(self._current_step_index(), "error")
        self.island.set_status("Cancelling…")
        if not self._confirm_event.is_set():
            # if a confirm dialog is mid-wait, treat Cancel as "No" so the
            # background thread doesn't sit blocked waiting for an answer
            self._confirm_result = False
            self.confirm_row.set_visible(False)
            self._confirm_event.set()

    def _current_step_index(self):
        for i, (_label, status) in enumerate(self.task_steps):
            if status == "current":
                return i
        return -1

    def _set_task_step_status(self, index, status):
        if 0 <= index < len(self.task_steps):
            label, _old = self.task_steps[index]
            self.task_steps[index] = (label, status)
        if self.island_expanded.get_visible():
            self.island_expanded.set_task(self.task_label_text, self.task_steps)
        self._sync_chibi_steps()
        self._update_island_progress()
        self._publish_remote_state()

    # -- animation driver --------------------------------------------------
    IDLE_FRAME_DIVISOR = 3   # idle breathing at a third of the frame rate

    def tick(self):
        # Nothing of the face is on screen while the pill is hidden, so do no
        # work at all; when it's idle, draw every third frame. Measured with
        # the pill open and idle, this took the app from ~18% of a core to
        # a few percent.
        if not self.get_visible() and not self._hiding:
            return True
        self._tick_count = getattr(self, "_tick_count", 0) + 1
        if not self.face.needs_full_frame_rate() and self._tick_count % self.IDLE_FRAME_DIVISOR:
            return True
        self._update_gaze_toward_cursor()
        self.face.tick(self.current_mood)
        if self.get_visible() and self.face.state == State.IDLE:
            if time.monotonic() - self.last_interaction > IDLE_TIMEOUT_S:
                self.face.set_state(State.SLEEPING)
        return True

    def _update_gaze_toward_cursor(self):
        if self.face.state != State.IDLE:
            return
        try:
            display = self.get_display()
            seat = display.get_default_seat()
            pointer = seat.get_pointer()
            face_window = self.face.get_window()
            if not pointer or not face_window:
                return
            _screen, px, py, _mods = pointer.get_position()
            alloc = self.face.get_allocation()
            fx, fy = self.face.translate_coordinates(self.get_toplevel(), 0, 0) or (0, 0)
            win_x, win_y = self.get_position() if hasattr(self, "get_position") else (0, 0)
            face_center_x = win_x + fx + alloc.width / 2
            face_center_y = win_y + fy + alloc.height / 2
            dx = (px - face_center_x) / 300.0
            dy = (py - face_center_y) / 300.0
            self.face.set_gaze_target(dx, dy)
        except Exception:
            pass  # cursor tracking is a nice-to-have, never worth crashing over

    def on_key(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.hide_panel()
        return False

    def on_typing(self, widget):
        self.last_interaction = time.monotonic()
        if self.face.state in (State.IDLE,):
            self.face.set_state(State.LISTENING)

    def on_face_button_press(self, widget, event):
        # Don't rely on GDK's automatic double-click detection (_2BUTTON_PRESS) —
        # it's been unreliable on gtk-layer-shell surfaces in testing. Track
        # double-clicks ourselves off plain BUTTON_PRESS events instead.
        if event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        self.face.tap()
        now = time.monotonic()
        last_click = getattr(self, "_last_face_click_time", 0.0)
        self._last_face_click_time = now
        if now - last_click < 0.45:
            self._last_face_click_time = 0.0  # consume it so a 3rd quick click doesn't re-trigger
            self.toggle_expanded()
        return False

    def toggle_expanded(self):
        self.expanded = not self.expanded
        self.expand_btn.set_label("\u25b4" if self.expanded else "\u25be")
        if self.expanded:
            self.history_scroller.set_max_content_height(650)
            self.history_scroller.set_min_content_height(600)
            self.refresh_dashboard()
            self.refresh_memory_graph()
            self.refresh_study_plan_grid()
            self.sidebar_window.set_visible(True)
            self.sidebar_window.present()
        else:
            self.sidebar_window.set_visible(False)
        self.input_shape_combine_region(None)

    def switch_nav(self, key):
        self.content_stack.set_visible_child_name(key)
        for k, btn in self.nav_buttons.items():
            ctx = btn.get_style_context()
            if k == key:
                ctx.add_class("nav-selected")
            else:
                ctx.remove_class("nav-selected")
        if key == "memories":
            self.refresh_memory_graph()
        elif key == "dashboard":
            self.refresh_dashboard()
        elif key == "plan":
            self.refresh_study_plan_grid()

    # -- Dashboard ------------------------------------------------------------
    def refresh_dashboard(self):
        status_map = {
            State.SLEEPING: "Sleeping", State.WAKING: "Waking up", State.IDLE: "Idle",
            State.LISTENING: "Listening", State.THINKING: "Thinking", State.RESPONDING: "Responding",
            State.SPEAKING: "Speaking",
        }
        self.dashboard_labels["status"].set_text(status_map.get(self.face.state, "Idle"))
        self.dashboard_labels["task"].set_text(self.task_label_text or "None")
        cpu = sample_cpu_percent()
        self.dashboard_labels["cpu"].set_text(f"{cpu:.0f}%" if cpu is not None else "…")
        mem = sample_mem_percent()
        self.dashboard_labels["mem"].set_text(f"{mem:.0f}%" if mem is not None else "…")
        self.dashboard_labels["context"].set_text(
            f"{min(len(self.history), MAX_HISTORY_TURNS)}/{MAX_HISTORY_TURNS} messages")
        self.dashboard_labels["tools"].set_text(str(len(DISPATCH)))
        automations = []
        if study_mode_state.active:
            automations.append("Study Mode ON")
        if self.school_scheduled_active:
            automations.append("School schedule active")
        if self.smart_mode_active:
            automations.append(f"Smart mode ON ({SETTINGS.get('cloud_model', 'cloud')})")
        elif SETTINGS.get("voice_auto_cloud") and SETTINGS.get("cloud_api_key", "").strip():
            automations.append("Voice requests use cloud")
        if self.voice.enabled:
            automations.append("Voice Mode listening")
        if self.camera.enabled:
            automations.append("Camera Mode on")
        self.dashboard_labels["automations"].set_text(", ".join(automations) if automations else "None")
        self.dashboard_labels["today"].set_text(str(self._todays_message_count()))
        return True  # keep repeating on the periodic timer

    def _todays_message_count(self):
        """How many exchanges happened today, counted sparingly.

        This reads the whole session log, and the Dashboard refreshes every
        two seconds. Recounting that often is pointless file I/O on the GTK
        thread for a number that barely moves.
        """
        now = time.monotonic()
        if now - getattr(self, "_today_count_at", 0.0) > 30:
            self._today_count = count_todays_sessions()
            self._today_count_at = now
        return getattr(self, "_today_count", 0)

    # -- Memories ---------------------------------------------------------------
    def refresh_memory_graph(self):
        layout = SETTINGS.get("memory_graph_layout", "force")
        data = knowledge.get_graph_data(layout)
        self.memory_graph.load_data(data["nodes"], data["edges"])

    def _export_graph_image(self, render):
        """Render one of the static graph images and open it.

        Drawing it goes through matplotlib and writing a PNG, which takes
        long enough to be noticed. Doing that in the button handler locked
        the interface up until the picture appeared.
        """
        self.memory_detail_label.set_text("Rendering the image…")

        def worker():
            try:
                message = render()
            except Exception as e:
                message = f"Couldn't render that: {e}"
            GLib.idle_add(self.memory_detail_label.set_text, message)

        threading.Thread(target=worker, daemon=True).start()

    def on_memory_layout_clicked(self, layout_key):
        """Switch between the three ways of arranging what Toby knows.

        Each answers a different question — the force layout shows which
        parts are densely related, the radial one compares categories by
        size, and the mind map is the one you can actually read down.
        """
        SETTINGS["memory_graph_layout"] = layout_key
        toby_settings.save(SETTINGS)
        for key, btn in self.memory_layout_buttons.items():
            context = btn.get_style_context()
            if key == layout_key:
                context.add_class("nav-selected")
            else:
                context.remove_class("nav-selected")
        self.memory_graph.reset_view()
        self.refresh_memory_graph()

    def _refresh_memories_if_showing(self):
        """Called from the background bookkeeping thread after fact extraction.
        Only redraws when the Memories page is actually on screen — no point
        rebuilding a graph nobody is looking at."""
        if self.expanded and self.content_stack.get_visible_child_name() == "memories":
            self.refresh_memory_graph()
        return False

    def on_memory_node_select(self, node):
        if node:
            self._selected_memory_id = node["id"]
            self.memory_detail_label.set_text(f"[{node['category']}] {node['label']}")
            self.memory_delete_btn.set_sensitive(True)
        else:
            self._selected_memory_id = None
            self.memory_detail_label.set_text("Click a memory to see it here.")
            self.memory_delete_btn.set_sensitive(False)

    def on_memory_delete_clicked(self, *_a):
        if self._selected_memory_id:
            knowledge.delete_fact(self._selected_memory_id)
            self._selected_memory_id = None
            self.memory_detail_label.set_text("Click a memory to see it here.")
            self.memory_delete_btn.set_sensitive(False)
            self.refresh_memory_graph()

    def on_memory_add_clicked(self, *_a):
        text = self.memory_add_text.get_text()
        category = self.memory_add_category.get_text() or "general"
        added = knowledge.add_fact_manual(text, category)
        if added:
            self.memory_add_text.set_text("")
            self.memory_add_category.set_text("")
            self.refresh_memory_graph()

    # -- Settings ---------------------------------------------------------------
    def on_settings_save_clicked(self, *_a):
        SETTINGS["response_style"] = self.settings_style_combo.get_active_id() or "balanced"
        SETTINGS["memory_enabled"] = self.settings_memory_switch.get_active()
        SETTINGS["notifications_enabled"] = self.settings_notif_switch.get_active()
        accent = self.settings_accent_entry.get_text().strip()
        if len(accent) == 7 and accent.startswith("#"):
            SETTINGS["accent_color"] = accent
            self.apply_accent_color(accent)
        SETTINGS["ollama_model"] = self.settings_model_entry.get_text().strip()
        apply_model_settings()
        SETTINGS["grade_level"] = self.settings_grade_combo.get_active_id() or "9th-10th"

        voice_should_be_on = self.settings_voice_switch.get_active()
        SETTINGS["voice_mode_enabled"] = voice_should_be_on
        SETTINGS["voice_wake_word_enabled"] = self.settings_wake_word_switch.get_active()
        SETTINGS["voice_name"] = self.settings_voice_combo.get_active_id() or "en-us"
        try:
            SETTINGS["voice_rate"] = int(self.settings_rate_entry.get_text().strip())
        except ValueError:
            pass
        try:
            SETTINGS["voice_pitch"] = int(self.settings_pitch_entry.get_text().strip())
        except ValueError:
            pass
        self.voice.wake_word_enabled = SETTINGS["voice_wake_word_enabled"]
        self.voice.voice = SETTINGS["voice_name"]
        self.voice.rate = SETTINGS["voice_rate"]
        self.voice.pitch = SETTINGS["voice_pitch"]
        if voice_should_be_on and not self.voice.enabled:
            self.voice.start()
            self.voice_row.set_visible(True)
        elif not voice_should_be_on and self.voice.enabled:
            self.voice.stop()
            self.voice_row.set_visible(False)

        SETTINGS["cloud_api_key"] = self.settings_cloud_key_entry.get_text().strip()
        SETTINGS["cloud_model"] = self.settings_cloud_model_entry.get_text().strip() or "gpt-4o"
        SETTINGS["voice_auto_cloud"] = self.settings_voice_auto_cloud_switch.get_active()
        animations_before = toby_anim.animation_settings(SETTINGS)
        SETTINGS["animations"] = {**SETTINGS.get("animations", {}), **self._collect_animation_settings()}
        self._apply_animation_settings(animations_before)
        SETTINGS["confirm_with_fingerprint"] = self.settings_fingerprint_switch.get_active()
        if SETTINGS["confirm_with_fingerprint"]:
            self._check_fingerprint_reader()

        toby_settings.save(SETTINGS)
        self.settings_save_status.set_text("Saved.")
        GLib.timeout_add_seconds(3, lambda: self.settings_save_status.set_text("") or False)

    # -- animation settings ------------------------------------------------------
    ANIMATION_SWITCHES = [
        ("enabled", "Animations"),
        ("fold_enabled", "Lid fold when the laptop sleeps"),
        ("chibi_enabled", "Toby walks out and does tasks on screen"),
        ("idle_enabled", "Idle life (breathing, blinking)"),
        ("desktop_reactions", "React when windows and workspaces change"),
        ("appear_enabled", "Fade in and out"),
        ("hypr_animations_enabled", "Toby-style window and workspace animations"),
    ]
    ANIMATION_SLIDERS = [
        # key, label, low, high, step, how to show the value
        ("fold_close_duration", "Fold speed", 0.3, 1.2, 0.02, lambda v: f"{v:.2f}s"),
        ("fold_strength", "Fold strength", 0.0, 1.5, 0.05, lambda v: f"{v:.0%}"),
        ("fold_perspective", "Fold perspective", 0.0, 2.0, 0.05, lambda v: f"{v:.0%}"),
        ("fold_blur", "Motion blur", 0.0, 2.0, 0.05, lambda v: f"{v:.0%}"),
        ("fold_zoom", "Shrink toward centre", 0.0, 0.2, 0.01, lambda v: f"{v:.0%}"),
        ("idle_intensity", "Idle liveliness", 0.0, 2.0, 0.05, lambda v: f"{v:.0%}"),
        ("interaction_intensity", "Tap reaction", 0.0, 2.0, 0.05, lambda v: f"{v:.0%}"),
        ("chibi_walk_speed", "Walking speed", 300.0, 2000.0, 50.0, lambda v: f"{v:.0f} px/s"),
    ]

    def _build_animation_settings(self, page):
        heading = Gtk.Label(label="Animations")
        heading.set_xalign(0)
        heading.get_style_context().add_class("apple-agent-section-heading")
        page.pack_start(heading, False, False, 6)
        current = toby_anim.animation_settings(SETTINGS)

        self.animation_switches = {}
        for key, label in self.ANIMATION_SWITCHES:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.pack_start(Gtk.Label(label=label), False, False, 0)
            switch = Gtk.Switch()
            switch.set_active(bool(SETTINGS.get("animations", {}).get(
                key, toby_anim.ANIMATION_DEFAULTS[key])))
            row.pack_end(switch, False, False, 0)
            page.pack_start(row, False, False, 0)
            self.animation_switches[key] = switch

        self.animation_sliders = {}
        grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        for i, (key, label, low, high, step, fmt) in enumerate(self.ANIMATION_SLIDERS):
            name = Gtk.Label(label=label)
            name.set_xalign(0)
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, low, high, step)
            scale.set_draw_value(False)
            scale.set_hexpand(True)
            scale.set_value(current[key])
            value = Gtk.Label(label=fmt(current[key]))
            value.set_xalign(1)
            value.set_width_chars(8)
            value.get_style_context().add_class("apple-agent-dashboard-title")
            scale.connect("value-changed", lambda sc, lbl=value, f=fmt: lbl.set_text(f(sc.get_value())))
            grid.attach(name, 0, i, 1, 1)
            grid.attach(scale, 1, i, 1, 1)
            grid.attach(value, 2, i, 1, 1)
            self.animation_sliders[key] = scale
        page.pack_start(grid, False, False, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        preview = Gtk.Button(label="Preview the fold")
        preview.get_style_context().add_class("apple-agent-panel-button")
        preview.connect("clicked", lambda *_: self._preview_fold())
        buttons.pack_start(preview, False, False, 0)
        defaults = Gtk.Button(label="Reset to defaults")
        defaults.get_style_context().add_class("apple-agent-panel-button")
        defaults.connect("clicked", lambda *_: self._reset_animation_controls())
        buttons.pack_start(defaults, False, False, 0)
        page.pack_start(buttons, False, False, 4)

        note = Gtk.Label(
            label="The fold plays when the laptop goes to sleep, and in reverse when it wakes. "
                  "Because the lid only reports closing when it's nearly shut, you'll mostly "
                  "see the unfold; Preview shows the whole thing, and `toby sleep` plays it "
                  "in full before suspending. Window animations change only the running "
                  "Hyprland and never your config.")
        note.set_xalign(0)
        note.set_line_wrap(True)
        note.get_style_context().add_class("apple-agent-dashboard-title")
        page.pack_start(note, False, False, 4)

    def _reset_animation_controls(self):
        for key, switch in self.animation_switches.items():
            switch.set_active(bool(toby_anim.ANIMATION_DEFAULTS[key]))
        for key, scale in self.animation_sliders.items():
            scale.set_value(toby_anim.ANIMATION_DEFAULTS[key])

    def _collect_animation_settings(self):
        values = {}
        for key, switch in self.animation_switches.items():
            values[key] = switch.get_active()
        for key, scale in self.animation_sliders.items():
            values[key] = round(float(scale.get_value()), 3)
        # opening takes a little longer than closing, as it did by default
        values["fold_open_duration"] = round(values["fold_close_duration"] * 1.16, 3)
        return values

    def _apply_animation_settings(self, before):
        """Push saved animation settings to everything that uses them."""
        after = toby_anim.animation_settings(SETTINGS)
        self.face.reload_animation_settings()
        self.chibi_director.reload(after)
        # the fold daemon re-reads settings on SIGHUP
        subprocess.Popen(["pkill", "-HUP", "-f", "toby_fold.py"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if after["hypr_animations_enabled"] != before["hypr_animations_enabled"] or (
                after["hypr_animations_enabled"]
                and after["hypr_animation_speed"] != before["hypr_animation_speed"]):
            def worker():
                if after["hypr_animations_enabled"]:
                    hypr_animations.apply(after["hypr_animation_speed"])
                else:
                    hypr_animations.restore()
            threading.Thread(target=worker, daemon=True).start()

    def _preview_fold(self):
        found = subprocess.run(["pkill", "-USR2", "-f", "toby_fold.py"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        if not found:
            self.settings_save_status.set_text("The fold animation isn't running — start it with: toby start")

    def apply_accent_color(self, hex_color):
        """Rebuild the whole stylesheet around a new accent colour.

        The accent used to be applied as a second, higher-priority provider
        holding a couple of overrides. That quietly undid the contrast the
        selected sidebar item was designed with, because the override was a
        fainter wash than the rule it replaced. Regenerating the one sheet
        keeps every accented state at its intended weight, and means a new
        accented component never needs a second rule written for it.
        """
        try:
            self.css_provider.load_from_data(build_css(hex_color).encode())
            set_accent_rgb(hex_color)
            self.waveform.queue_draw()
            self.memory_graph.queue_draw()
        except Exception as e:
            print("ACCENT COLOR ERROR:", e, flush=True)

    def _append_history_row(self, user_text, reply_text):
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        row.get_style_context().add_class("apple-agent-history-row")
        user_label = Gtk.Label(label=f"You: {user_text}")
        user_label.set_line_wrap(True)
        user_label.set_xalign(0)
        user_label.get_style_context().add_class("apple-agent-history-user")
        row.pack_start(user_label, False, True, 0)
        reply_label = Gtk.Label(label=reply_text)
        reply_label.set_line_wrap(True)
        reply_label.set_xalign(0)
        reply_label.get_style_context().add_class("apple-agent-history-reply")
        row.pack_start(reply_label, False, True, 0)
        self.history_box.pack_start(row, False, True, 0)
        row.show_all()

    # -- School Mode: scheduled + adaptive activation -----------------------
    def check_school_schedule(self):
        in_schedule = school_mode.is_in_schedule(self.school_mode_config)
        if in_schedule and not self.school_scheduled_active:
            study_mode_state.enable()
            self.school_scheduled_active = True
        elif not in_schedule and self.school_scheduled_active:
            study_mode_state.disable()
            self.school_scheduled_active = False
        return True

    def check_school_adaptive(self):
        if not self.school_mode_config.get("adaptive_enabled"):
            return True
        if study_mode_state.active:
            return True  # already on; no reason to read the screen at all
        if getattr(self, "_adaptive_check_running", False):
            return True  # the previous one hasn't finished yet

        # Screenshot plus OCR takes seconds. This runs on a timer on the GTK
        # main thread, so doing it inline froze the whole interface every
        # five minutes for no reason the user could see.
        self._adaptive_check_running = True
        keywords = self.school_mode_config.get("adaptive_keywords", [])

        def worker():
            try:
                text = capture_screen_text()
                if school_mode.screen_matches_keywords(text, keywords):
                    GLib.idle_add(study_mode_state.enable)
            except Exception as e:
                print("SCHOOL MODE ADAPTIVE CHECK ERROR:", e, flush=True)
            finally:
                self._adaptive_check_running = False

        threading.Thread(target=worker, daemon=True).start()
        return True

    # -- Study Mode: helper button actions, each a single explicit click ------
    def refresh_study_helper_stats(self):
        stats = study_mode_state.session_stats
        elapsed = ""
        if stats["session_start"] is not None:
            mins = int((time.monotonic() - stats["session_start"]) / 60)
            elapsed = f" — {mins}m"
        self.study_helper.set_stats(
            f"{stats['summaries']} summaries, {stats['quizzes_taken']} quizzes, "
            f"{stats['flashcards_reviewed']} flashcard sets{elapsed}"
        )
        return True

    def on_study_summarize(self):
        self.study_helper.set_status("Reading your screen…")

        def worker():
            try:
                text = capture_screen_text()
                source_label = get_active_window_context() or "Screen capture"
                result = study_notes.summarize_material(text, source_label)
            except Exception as e:
                GLib.idle_add(self.study_helper.set_status, f"Summarize failed: {e}")
                return
            if result.get("is_assignment"):
                GLib.idle_add(self.study_helper.set_status, "That looks like an assignment — ask me about the concepts instead.")
            elif result.get("saved"):
                study_mode_state.session_stats["summaries"] += 1
                GLib.idle_add(self.study_helper.set_status, f"Saved: {result.get('summary', '')[:80]}")
                GLib.idle_add(self.face.pulse_happy)
            else:
                GLib.idle_add(self.study_helper.set_status, "Couldn't find anything clear to summarize.")

        threading.Thread(target=worker, daemon=True).start()

    def on_study_quiz(self):
        self.study_helper.set_status("Building a quiz from your notes…")

        def worker():
            items = study_notes.generate_quiz(count=5)
            study_mode_state.session_stats["quizzes_taken"] += 1
            GLib.idle_add(self.study_helper.set_status, "" if items else "No notes to quiz on yet.")
            GLib.idle_add(self.study_review.open_review, "quiz", items)

        threading.Thread(target=worker, daemon=True).start()

    def on_study_flashcards(self):
        self.study_helper.set_status("Building flashcards from your notes…")

        def worker():
            items = study_notes.generate_flashcards(count=8)
            study_mode_state.session_stats["flashcards_reviewed"] += 1
            GLib.idle_add(self.study_helper.set_status, "" if items else "No notes to make cards from yet.")
            GLib.idle_add(self.study_review.open_review, "flashcards", items)

        threading.Thread(target=worker, daemon=True).start()

    # -- Study Plan (proficiency grid, material upload) -----------------------
    def refresh_study_plan_grid(self):
        for child in list(self.plan_grid.get_children()):
            self.plan_grid.remove(child)
        for topic in study_plan.get_all_topics():
            prof = topic.get("proficiency")
            if prof is None:
                tier = "none"
                label_text = f"{topic['topic']}\n({topic['subject']})\nNot assessed"
            elif prof < 50:
                tier = "low"
                label_text = f"{topic['topic']}\n({topic['subject']})\n{prof}%"
            elif prof < 80:
                tier = "mid"
                label_text = f"{topic['topic']}\n({topic['subject']})\n{prof}%"
            else:
                tier = "high"
                label_text = f"{topic['topic']}\n({topic['subject']})\n{prof}%"
            btn = Gtk.Button(label=label_text)
            btn.get_style_context().add_class("apple-agent-topic-card")
            btn.get_style_context().add_class(f"apple-agent-topic-{tier}")
            btn.connect("clicked", lambda _b, t=topic: self.on_topic_card_clicked(t))
            self.plan_grid.add(btn)
        self.plan_grid.show_all()

    def on_topic_card_clicked(self, topic):
        self.topic_detail.open_topic(topic)

    def on_topic_quiz_requested(self, subject, topic_name):
        self.topic_detail.set_visible(False)
        self.study_helper.set_status(f"Building a quiz on {topic_name}…")

        def worker():
            items = study_notes.generate_quiz(count=5, subject=subject)
            GLib.idle_add(self.study_helper.set_status, "" if items else "No notes on this subject to quiz from yet.")
            GLib.idle_add(self.study_review.open_review, "quiz", items, subject, topic_name)

        threading.Thread(target=worker, daemon=True).start()

    def on_quiz_finished(self, subject, topic_name, score, total):
        study_plan.record_quiz_result(subject, topic_name, score / total if total else 0)
        if self.content_stack.get_visible_child_name() == "plan":
            self.refresh_study_plan_grid()

    def on_upload_material_clicked(self, *_a):
        dialog = Gtk.FileChooserDialog(
            title="Upload study material", parent=self, action=Gtk.FileChooserAction.OPEN
        )
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OPEN, Gtk.ResponseType.OK)
        file_filter = Gtk.FileFilter()
        file_filter.set_name("PDF and text files")
        file_filter.add_pattern("*.pdf")
        file_filter.add_pattern("*.txt")
        file_filter.add_pattern("*.md")
        dialog.add_filter(file_filter)
        response = dialog.run()
        filepath = dialog.get_filename() if response == Gtk.ResponseType.OK else None
        dialog.destroy()
        if not filepath:
            return

        self.upload_status_label.set_text("Reading and analyzing…")

        def worker():
            result = study_plan.ingest_material(filepath, OLLAMA_URL, SETTINGS.get("ollama_model") or OLLAMA_MODEL)
            if result.get("error"):
                GLib.idle_add(self.upload_status_label.set_text, result["error"])
                return
            if result.get("is_assignment"):
                msg = f"Tracked subject '{result['subject']}' from this assignment (didn't extract answers)."
            else:
                topics_str = ", ".join(result.get("topics", []))
                msg = f"Added {result['subject']}: {topics_str}"
            GLib.idle_add(self.upload_status_label.set_text, msg)
            GLib.idle_add(self.refresh_study_plan_grid)
            GLib.idle_add(self.face.pulse_happy)

        threading.Thread(target=worker, daemon=True).start()

    # -- Voice Mode -------------------------------------------------------------
    def on_mic_button_press(self, widget, event):
        self._mic_holding = False

        def mark_holding():
            self._mic_holding = True
            self._mic_hold_timeout_id = None
            if not self.voice.enabled:
                self.voice.start()
                self.voice_row.set_visible(True)
            self.voice.push_to_talk_start()
            return False

        self._mic_hold_timeout_id = GLib.timeout_add(350, mark_holding)
        return True

    def on_mic_button_release(self, widget, event):
        if self._mic_hold_timeout_id:
            GLib.source_remove(self._mic_hold_timeout_id)
            self._mic_hold_timeout_id = None
        if self._mic_holding:
            self.voice.push_to_talk_stop()
            self._mic_holding = False
            if not SETTINGS.get("voice_mode_enabled", False):
                self.voice.stop()
                self.voice_row.set_visible(False)
        else:
            # a quick tap (not a hold) toggles persistent Voice Mode
            new_state = not SETTINGS.get("voice_mode_enabled", False)
            SETTINGS["voice_mode_enabled"] = new_state
            toby_settings.save(SETTINGS)
            if new_state:
                self.voice.start()
                self.voice_row.set_visible(True)
            else:
                self.voice.stop()
                self.voice_row.set_visible(False)
        return True

    def on_voice_partial(self, text):
        GLib.idle_add(self.voice_transcript_label.set_text, text)

    def on_voice_final(self, text, confidence):
        GLib.idle_add(self._handle_voice_command, text, confidence)

    def _handle_voice_command(self, text, confidence):
        word_count = len(text.split())
        if confidence < VOICE_CONFIDENCE_THRESHOLD or word_count < VOICE_MIN_WORDS:
            # Likely background noise/TV/music mis-heard as speech — show it was
            # picked up (so it's not a silent mystery) but don't act on it.
            self.voice_transcript_label.set_text(
                f'(ignored, low confidence) "{text}" ({int(confidence * 100)}%)'
            )
            return False
        self.voice_transcript_label.set_text(f'"{text}" ({int(confidence * 100)}% confidence)')
        if not self.get_visible():
            self.show_panel()
        self.entry.set_text(text)
        force_cloud = SETTINGS.get("voice_auto_cloud", False) and bool(SETTINGS.get("cloud_api_key", "").strip())
        self.on_submit(self.entry, force_cloud=force_cloud)
        return False

    def on_voice_level(self, level):
        GLib.idle_add(self.waveform.push_level, level)
        GLib.idle_add(self.face.set_audio_level, level)

    def on_voice_state(self, state_str):
        GLib.idle_add(self._apply_voice_state, state_str)

    def _apply_voice_state(self, state_str):
        if state_str.startswith("error"):
            self.voice_transcript_label.set_text(state_str)
            return False
        if state_str in ("listening_wake", "listening_command"):
            self.face.set_state(State.LISTENING)
            self.face.set_speaking(False)
            self.voice_transcript_label.set_text(
                "Listening for wake word…" if state_str == "listening_wake" else "Listening…"
            )
        elif state_str == "speaking":
            self.face.set_state(State.SPEAKING)
            self.face.set_speaking(True)
        elif state_str == "idle":
            self.face.set_speaking(False)
            if self.face.state in (State.LISTENING, State.SPEAKING):
                self.face.set_state(State.IDLE)
        return False

    # -- Camera Mode --------------------------------------------------------------
    def on_smart_toggled(self, widget):
        wants_on = widget.get_active()
        if wants_on and not SETTINGS.get("cloud_api_key", "").strip():
            # Don't leave the button looking switched on while every request
            # quietly goes to the local model anyway. Switch it back and say
            # why — the state the user can see should be the state that is
            # actually in effect.
            self.smart_mode_active = False
            widget.handler_block_by_func(self.on_smart_toggled)
            widget.set_active(False)
            widget.handler_unblock_by_func(self.on_smart_toggled)
            self._show_notice("Smart mode needs an API key. Add one under "
                              "Settings, Cloud AI, then try again.")
            return
        self.smart_mode_active = wants_on
        self._show_notice("Smart mode on — messages go to your cloud model."
                          if wants_on else "Smart mode off — back to the local model.")

    def _show_notice(self, text):
        """Put a short message in the answer line, where the user is looking."""
        self.answer.set_text(text)
        self.answer.set_visible(True)
        self.answer.show()

    def on_camera_button_clicked(self, *_a):
        new_state = not SETTINGS.get("camera_mode_enabled", False)
        SETTINGS["camera_mode_enabled"] = new_state
        toby_settings.save(SETTINGS)
        self.settings_camera_switch.handler_block_by_func(self._on_camera_switch_toggled)
        self.settings_camera_switch.set_active(new_state)
        self.settings_camera_switch.handler_unblock_by_func(self._on_camera_switch_toggled)
        if new_state:
            self.camera.start()
            self.camera_overlay.set_visible(True)
            if SETTINGS.get("camera_pinch_cursor", False):
                self._arm_pinch_cursor()
        else:
            self.camera.stop()
            self._disarm_pinch_cursor()
            self.camera_overlay.set_visible(False)

    def _on_camera_switch_toggled(self, widget, state):
        SETTINGS["camera_mode_enabled"] = bool(state)
        toby_settings.save(SETTINGS)
        if state:
            self.camera.start()
            self.camera_overlay.set_visible(True)
            if SETTINGS.get("camera_pinch_cursor", False):
                self._arm_pinch_cursor()
        else:
            self.camera.stop()
            self._disarm_pinch_cursor()
            self.camera_overlay.set_visible(False)
        return False

    # -- pinch to point ---------------------------------------------------------
    # Tuned against how MediaPipe reports a hand rather than picked at random:
    # PINCH_ON/PINCH_OFF are thumb-to-index distance as a fraction of the
    # hand's own size, with a gap between them so a hand hovering near the
    # threshold doesn't rattle between clicking and not. The dead zone crops
    # the edges of the camera's view, because the far edges of the frame are
    # both hard to reach and where tracking gets least reliable.
    PINCH_ON = 0.38
    PINCH_OFF = 0.55
    POINT_SMOOTHING = 0.45   # 0 = no movement, 1 = no smoothing at all
    POINT_DEAD_ZONE = 0.12   # fraction of the frame ignored at each edge
    PINCH_CLICK_COOLDOWN_S = 0.6

    def on_camera_hand_frame(self, hands_xy):
        GLib.idle_add(self.camera_overlay.set_hands, hands_xy)
        if self._pinch_cursor_armed:
            self._drive_pointer_from_hand(hands_xy)

    def _drive_pointer_from_hand(self, hands_xy):
        """Point with your index finger to move the cursor, pinch to click.

        Runs on the camera thread. Nothing here blocks: the actual pointer
        moves are handed to PointerDriver, which has a thread of its own.
        """
        if not hands_xy:
            self._pinch_held = False
            return
        points = hands_xy[0]
        if len(points) < 21:
            return

        wrist, thumb_tip = points[0], points[4]
        index_tip, middle_mcp = points[8], points[9]
        hand_size = math.dist(wrist, middle_mcp) or 0.001
        pinch_distance = math.dist(thumb_tip, index_tip) / hand_size

        # A gap between the on and off thresholds, so a hand resting near the
        # boundary doesn't fire a stream of clicks.
        if self._pinch_held:
            if pinch_distance > self.PINCH_OFF:
                self._pinch_held = False
        elif pinch_distance < self.PINCH_ON:
            self._pinch_held = True
            now = time.monotonic()
            if now - self._last_pinch_click >= self.PINCH_CLICK_COOLDOWN_S:
                self._last_pinch_click = now
                pointer_driver.request_click()
                GLib.idle_add(self.camera_overlay.set_gesture_flash, "click")

        # The camera sees a mirror image, so moving your hand right has to
        # move the cursor right, not left.
        span = 1.0 - 2 * self.POINT_DEAD_ZONE
        x = (1.0 - index_tip[0] - self.POINT_DEAD_ZONE) / span
        y = (index_tip[1] - self.POINT_DEAD_ZONE) / span
        x = max(0.0, min(1.0, x))
        y = max(0.0, min(1.0, y))

        if self._pointer_smoothed is None:
            self._pointer_smoothed = (x, y)
        else:
            prev_x, prev_y = self._pointer_smoothed
            k = self.POINT_SMOOTHING
            self._pointer_smoothed = (prev_x + (x - prev_x) * k,
                                      prev_y + (y - prev_y) * k)
        pointer_driver.aim(*self._pointer_smoothed)

        if pointer_driver.stopped() and pointer_driver.last_error:
            GLib.idle_add(self._disarm_pinch_cursor, pointer_driver.last_error)

    def _arm_pinch_cursor(self):
        """Turn on hand-pointing, asking for mouse consent first if needed."""
        if self._pinch_cursor_armed:
            return
        if not screen_control.enabled:
            if self._action_confirm_pending:
                # Something else is already waiting on the Yes/No row, and a
                # background thread is blocked until it is answered. Don't
                # replace that question with this one.
                self.camera_overlay.set_visible(True)
                self.camera_overlay.set_recording(
                    "Answer the pending confirmation first, then turn hand pointing on again")
                return
            # Same one-time confirmation any other pointer action needs. It
            # has to be answered before a hand can move the real cursor.
            self.confirm_label.set_text("Let Toby move the cursor with your hand?")
            self._pending_confirm_purpose = "pinch_cursor"
            self._show_confirm_dialog()
            return
        self._pinch_cursor_armed = True
        self._pointer_smoothed = None
        self._pinch_held = False
        pointer_driver.last_error = ""
        pointer_driver.start()
        self.camera_overlay.set_visible(True)
        self.camera_overlay.set_recording("Pointing: index finger moves the cursor, pinch to click")

    def _disarm_pinch_cursor(self, reason=""):
        self._pinch_cursor_armed = False
        self._pointer_smoothed = None
        self._pinch_held = False
        pointer_driver.stop()
        if reason:
            self.camera_overlay.set_recording(reason)
        return False

    def _on_pinch_cursor_switch_toggled(self, widget, state):
        SETTINGS["camera_pinch_cursor"] = bool(state)
        toby_settings.save(SETTINGS)
        if state:
            if not self.camera.enabled:
                self.camera_overlay.set_visible(True)
                self.camera_overlay.set_recording("Turn on Camera Mode to use hand pointing")
            self._arm_pinch_cursor()
        else:
            self._disarm_pinch_cursor("Hand pointing off")
        return False

    def on_camera_state(self, state_str):
        GLib.idle_add(self._apply_camera_state, state_str)

    def _apply_camera_state(self, state_str):
        if state_str.startswith("unavailable") or state_str.startswith("error"):
            self.camera_overlay.status_label.set_text(state_str)
            self.camera_overlay.set_visible(True)
            GLib.timeout_add_seconds(6, lambda: self.camera_overlay.set_visible(False) or False)
        return False

    def _camera_action(self, action_key):
        """The fixed set of real actions any built-in or custom gesture can trigger."""
        handlers = {
            "toggle_panel": self.toggle,
            "hide_panel": self.hide_panel,
            "toggle_expanded": self.toggle_expanded,
            "confirm_yes": lambda: self.on_confirm_yes() if self.confirm_row.get_visible() else None,
            "confirm_no": lambda: self.on_confirm_no() if self.confirm_row.get_visible() else None,
            # This compositor's hyprlua config parses dispatch args as Lua
            # expressions, not plain strings — "e-1"/"e+1" needs to arrive
            # pre-quoted as a Lua string literal, or it errors trying to
            # evaluate "e+1" as an expression (same class of issue as
            # close_active_window()'s "killactive" workaround above).
            "workspace_prev": lambda: subprocess.Popen(["hyprctl", "dispatch", "workspace", '"e-1"']),
            "workspace_next": lambda: subprocess.Popen(["hyprctl", "dispatch", "workspace", '"e+1"']),
            "toggle_fullscreen": lambda: subprocess.Popen(["hyprctl", "dispatch", "fullscreen", "1"]),
            "cycle_nav": self._cycle_nav_section,
            "pulse_happy": self.face.pulse_happy,
            "expand_island": lambda: self.on_island_expand() if self.island.get_visible() else None,
            "close_island_expanded": self.island_expanded.close,
        }
        fn = handlers.get(action_key)
        if fn:
            try:
                fn()
            except Exception as e:
                print("CAMERA ACTION ERROR:", e, flush=True)

    def _cycle_nav_section(self):
        if not self.expanded:
            return
        order = list(self.nav_buttons.keys())
        current = self.content_stack.get_visible_child_name()
        next_key = order[(order.index(current) + 1) % len(order)] if current in order else "dashboard"
        self.switch_nav(next_key)

    # Built-in gesture -> action. Not every detected gesture needs a binding —
    # blink/head_tilt are detected and available (including to future custom
    # gestures) but deliberately left unbound here to avoid the face firing
    # actions on every incidental blink.
    CAMERA_GESTURE_ACTIONS = {
        "open_palm": "toggle_panel",
        "fist": "hide_panel",
        "swipe_left": "workspace_prev",
        "swipe_right": "workspace_next",
        "push_forward": "confirm_yes",
        "pull_backward": "confirm_no",
        "spread_fingers": "toggle_fullscreen",
        "rotate_cw": "cycle_nav",
        "rotate_ccw": "cycle_nav",
        "double_blink": "confirm_yes",
        "head_shake": "confirm_no",
        "eyebrows_raised": "toggle_expanded",
        "smile": "pulse_happy",
        "gaze_right": "expand_island",
        "gaze_left": "close_island_expanded",
    }

    def on_camera_gesture(self, name):
        GLib.idle_add(self._handle_camera_gesture, name)

    def _handle_camera_gesture(self, name):
        if name.startswith("__recorded__:"):
            gesture_name = name.split(":", 1)[1]
            self.camera_overlay.set_recording(f"Recorded '{gesture_name}'")
            self._camera_recording_name = None
            self.refresh_camera_gesture_list()
            return False
        if name.startswith("__pinch_drag__:"):
            if self._pinch_cursor_armed:
                # a pinch means "click" while hand pointing is on; it must not
                # also drag the panel around
                return False
            _tag, x_str, y_str = name.split(":")
            if self.get_visible():
                try:
                    y = float(y_str)
                    margin = int(20 + (1.0 - y) * 400)  # drag hand up -> panel rises
                    GtkLayerShell.set_margin(self, GtkLayerShell.Edge.BOTTOM, margin)
                except ValueError:
                    pass
            return False
        if name.startswith("custom:"):
            gesture_name = name.split(":", 1)[1]
            match = next((g for g in self.camera.custom_gestures if g["name"] == gesture_name), None)
            self.camera_overlay.set_gesture_flash(f"Gesture: {gesture_name}")
            if match and match.get("action"):
                self._camera_action(match["action"])
            return False

        self.camera_overlay.set_gesture_flash(name.replace("_", " "))
        action_key = self.CAMERA_GESTURE_ACTIONS.get(name)
        if action_key:
            self._camera_action(action_key)
        return False

    def refresh_camera_gesture_list(self):
        for child in self.camera_gesture_list_box.get_children():
            self.camera_gesture_list_box.remove(child)
        for g in self.camera.custom_gestures:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            label = Gtk.Label(label=f"{g['name']} \u2192 {g.get('action', '(none)')}")
            label.set_xalign(0)
            row.pack_start(label, True, True, 0)
            del_btn = Gtk.Button(label="Delete")
            del_btn.get_style_context().add_class("apple-agent-panel-button")
            del_btn.connect("clicked", lambda _b, n=g["name"]: self._delete_custom_gesture(n))
            row.pack_start(del_btn, False, False, 0)
            self.camera_gesture_list_box.pack_start(row, False, False, 0)
        self.camera_gesture_list_box.show_all()

    def _delete_custom_gesture(self, name):
        self.camera.delete_custom_gesture(name)
        self.refresh_camera_gesture_list()

    def on_camera_record_clicked(self, *_a):
        name = self.camera_gesture_name_entry.get_text().strip()
        action_key = self.camera_gesture_action_combo.get_active_id()
        if not name or not action_key:
            self.camera_overlay.set_recording("Enter a name and pick an action first")
            return
        if not self.camera.enabled:
            self.camera_overlay.set_recording("Turn on Camera Mode first")
            return
        self._pending_camera_action_for_record = action_key
        self.camera.start_recording(name)
        self.camera_overlay.set_visible(True)
        self.camera_overlay.set_recording(f"Recording '{name}'… hold the pose")

        def check_done():
            if self.camera.recording_gesture is None:
                if self._pending_camera_action_for_record:
                    for g in self.camera.custom_gestures:
                        if g["name"] == name and "action" not in g:
                            g["action"] = self._pending_camera_action_for_record
                            self.camera._save_gestures()
                    self._pending_camera_action_for_record = None
                    self.refresh_camera_gesture_list()
                return False
            return True

        GLib.timeout_add(200, check_done)

    # -- submit / process ----------------------------------------------------
    def on_submit(self, widget, force_cloud=False):
        text = self.entry.get_text().strip()
        if not text:
            return
        self.entry.set_text("")
        self._busy = True
        self._remote_reply = ""
        self.face.set_state(State.THINKING)
        self.answer.set_text("")
        self.task_cancelled = False
        will_use_cloud = (self.smart_mode_active or force_cloud) and SETTINGS.get("cloud_api_key", "").strip()
        smart_tag = "[Smart] " if will_use_cloud else ""
        self.task_label_text = smart_tag + (text if len(text) <= 60 else text[:57] + "…")
        self.task_steps = [("Thinking", "current")]
        self._voice_spoken_len = 0
        if self.island_expanded.get_visible():
            self.island_expanded.set_task(self.task_label_text, self.task_steps)

        # Immediate, unmissable "it's working on it" feedback — bouncing dots
        # right away, and the Dynamic Island pops up after a short delay
        # REGARDLESS of whether the panel is open, so a slow response is
        # never silent. Both clear themselves the moment real text starts
        # streaming in (see update_streaming_answer / finish_response).
        self._request_id = getattr(self, "_request_id", 0) + 1
        my_request_id = self._request_id
        self._waiting_for_first_chunk = True
        self._start_thinking_dots()
        GLib.timeout_add(1200, lambda: self._maybe_show_thinking_island(my_request_id))

        self._publish_remote_state(force=True)

        # Run on a background thread — process()/think() do blocking network
        # I/O, and running that on the GTK main thread would freeze the whole
        # UI (including the close button) for the duration of the request.
        # process() only ever touches widgets via GLib.idle_add, so this is safe.
        threading.Thread(target=self.process, args=(text, force_cloud), daemon=True).start()

    def _start_thinking_dots(self):
        self._dot_frame = 0

        def cycle():
            if not self._waiting_for_first_chunk:
                return False
            self._dot_frame = (self._dot_frame + 1) % 4
            self.answer.set_text("Thinking" + "." * self._dot_frame)
            self.answer.set_visible(True)
            self.answer.show()
            return True

        GLib.timeout_add(400, cycle)

    def _maybe_show_thinking_island(self, request_id):
        if request_id == self._request_id and self._waiting_for_first_chunk:
            self.island.show_island("Toby is thinking…")
        return False

    def process(self, text, force_cloud=False):
        global last_screen_context

        def on_chunk(content):
            GLib.idle_add(self.update_streaming_answer, content)
            GLib.idle_add(self.update_thinking_raw, content)
            # Gate on the engine actually running, not on the saved setting —
            # push-to-talk starts the engine without ever flipping the setting,
            # so keying off the setting left held-mic replies silent.
            if self.voice.enabled:
                partial_reply = extract_partial_reply(content)
                if partial_reply:
                    unspoken = partial_reply[self._voice_spoken_len:]
                    last_break = max(unspoken.rfind(". "), unspoken.rfind("! "),
                                      unspoken.rfind("? "), unspoken.rfind(".\n"))
                    if last_break != -1:
                        to_speak = unspoken[:last_break + 1].strip()
                        if to_speak:
                            self.voice.speak(to_speak)
                        self._voice_spoken_len += last_break + 1

        def cancel_check():
            return self.task_cancelled

        has_key = bool(SETTINGS.get("cloud_api_key", "").strip())
        use_cloud = (self.smart_mode_active or force_cloud) and has_key
        think_fn = think_cloud if use_cloud else think

        # Obvious one-step requests skip the model entirely (fast_path.py).
        quick = (fast_path.match(text, ALLOWED_APPS)
                 if SETTINGS.get("fast_path_enabled", True) and not use_cloud else None)
        if quick is not None:
            if self.voice.enabled:
                self.voice.speak(quick["reply"])
                self._voice_spoken_len = len(quick["reply"])
            actions_succeeded = self._execute_actions(quick["actions"])
            GLib.idle_add(self.finish_response, text, quick, actions_succeeded)
            return False
        if use_cloud:
            GLib.idle_add(self.island.show_island, "Asking the cloud AI…")

        if not self.get_visible():
            GLib.idle_add(self.island.show_island, "Toby is thinking…")

        try:
            result = think_fn(text, self.history, on_chunk=on_chunk, cancel_check=cancel_check)

            if result.get("_cancelled"):
                GLib.idle_add(self.finish_cancelled)
                return False

            wants_screen = any(a.get("tool") == "read_screen" for a in result.get("actions", []))
            if wants_screen and not self.task_cancelled:
                GLib.idle_add(self.island.show_island, "Reading your screen…")
                GLib.idle_add(self._set_task_step_status, 0, "done")
                screen_text = capture_screen_text()
                last_screen_context = screen_text
                augmented = (
                    text
                    + "\n\n[SCREEN CONTENT CAPTURED VIA OCR, for context only]:\n"
                    + screen_text[:3000]
                    + "\n\nNow answer the user's original request above using this "
                    + "screen content where relevant. Do not call read_screen again."
                )
                result = think_fn(augmented, self.history, on_chunk=on_chunk, cancel_check=cancel_check)
                if result.get("_cancelled"):
                    GLib.idle_add(self.finish_cancelled)
                    return False
        except Exception as e:
            result = {"reply": f"Ollama error: {e}", "actions": [], "mood": "neutral"}
            GLib.idle_add(self.finish_response, text, result, 0)
            return False

        # Actions run right here, still on this background thread — some of
        # them (mouse/keyboard via ydotool, subprocess calls in general) can
        # block or hang (e.g. if ydotoold isn't running), and this must never
        # be allowed to freeze the GTK main thread. When an action needs
        # confirmation, we idle_add the dialog onto the main thread and then
        # wait on an Event here — that blocks only this background thread,
        # not the UI, while the user decides.
        actions_succeeded = self._execute_actions(result.get("actions", []))
        GLib.idle_add(self.finish_response, text, result, actions_succeeded)
        return False

    def _execute_actions(self, actions):
        actions_succeeded = 0
        GLib.idle_add(self._set_task_step_status, 0, "done")  # "Thinking" step complete
        GLib.idle_add(self._extend_task_steps, actions)

        if self._chibi_enabled() and any(a.get("tool") in PHYSICAL_TOOLS for a in actions):
            GLib.idle_add(self._chibi_begin)
            time.sleep(toby_anim.animation_settings(SETTINGS)["chibi_transform_duration"] * 0.8)

        for i, action in enumerate(actions):
            if self.task_cancelled:
                GLib.idle_add(self._set_task_step_status, i + 1, "error")
                break
            GLib.idle_add(self._set_task_step_status, i + 1, "current")
            fn = DISPATCH.get(action.get("tool"))
            if not fn:
                GLib.idle_add(self._set_task_step_status, i + 1, "error")
                continue
            try:
                self._chibi_choreograph(action)
            except Exception as e:
                print("CHIBI ERROR:", e, flush=True)   # never let the show stop the task
            try:
                result_summary = fn(action)
                note_recent_action(action.get("tool", "?"), str(result_summary)[:80])
                actions_succeeded += 1
                GLib.idle_add(self._set_task_step_status, i + 1, "done")
                if action.get("tool") == "set_school_schedule":
                    self.school_mode_config = school_mode.load_config()
            except NeedsConfirmation:
                self._confirm_event.clear()
                self._action_confirm_pending = True
                GLib.idle_add(self._show_confirm_dialog)
                self._confirm_event.wait()  # blocks this background thread only, never the UI
                if not self._confirm_result:
                    GLib.idle_add(self._set_task_step_status, i + 1, "error")
                    break
                try:
                    result_summary = fn(action)
                    note_recent_action(action.get("tool", "?"), str(result_summary)[:80])
                    actions_succeeded += 1
                    GLib.idle_add(self._set_task_step_status, i + 1, "done")
                except Exception as e:
                    GLib.idle_add(self._set_task_step_status, i + 1, "error")
                    print("ACTION ERROR:", e, flush=True)
            except Exception as e:
                GLib.idle_add(self._set_task_step_status, i + 1, "error")
                print("ACTION ERROR:", e, flush=True)

        return actions_succeeded

    def _extend_task_steps(self, actions):
        self.task_steps += [(describe_action(a), "pending") for a in actions]
        if self.island_expanded.get_visible():
            self.island_expanded.set_task(self.task_label_text, self.task_steps)
        self._sync_chibi_steps()
        return False

    # -- the chibi doing the task ----------------------------------------------
    def _update_island_progress(self):
        """While a multi-step task runs, the island shows where it's up to —
        including when the panel is closed or the task came from the phone."""
        steps = [st for st in self.task_steps if st[0] != "Thinking"]
        if not steps or not self._busy:
            self.island.set_progress(0, 0)
            return
        done = sum(1 for _l, status in steps if status == "done")
        current = next((label for label, status in steps if status == "current"), "")
        self.island.set_progress(done, len(steps), current)
        if not self.get_visible() and not self.island.showing_card():
            self.island.show_island(self.island.label.get_text())

    def _chibi_enabled(self):
        return toby_anim.animation_settings(SETTINGS)["chibi_enabled"]

    def _sync_chibi_steps(self):
        """The chibi's checklist is the task list minus the 'Thinking' step."""
        if self.chibi_director.visible:
            steps = [s for s in self.task_steps if s[0] != "Thinking"]
            self.chibi_director.set_steps(self._chibi_title(), steps)
            self.chibi_stage.start()
        return False

    def _chibi_title(self):
        title = self.task_label_text.replace("[Smart] ", "")
        return title[:1].upper() + title[1:] if title else "Working on it"

    def _face_screen_position(self):
        """Where the face sits on screen, so the chibi pops out of it."""
        screen = self.get_screen()
        sw, sh = screen.get_width(), screen.get_height()
        if not self.get_visible():
            return sw / 2, sh - 40
        try:
            fx, fy = self.face.translate_coordinates(self, 0, 0) or (0, 0)
            win_w = self.get_allocated_width()
            win_h = self.get_allocated_height()
            face = self.face.get_allocation()
            x = (sw - win_w) / 2 + fx + face.width / 2
            y = sh - 55 - win_h + fy + face.height
            return x, y
        except Exception:
            return sw / 2, sh - 80

    def _chibi_begin(self):
        """The face leaves the pill and grows a body on the stage."""
        if self.chibi_director.visible:
            return False
        x, y = self._face_screen_position()
        steps = [s for s in self.task_steps if s[0] != "Thinking"]
        self.chibi_director.reload(toby_anim.animation_settings(SETTINGS))
        self.chibi_director.emerge(x, y, self._chibi_title(), steps)
        self.chibi_stage.start()
        self.face.set_state(State.SLEEPING)   # the head has left the pill
        return False

    def _chibi_end(self, reply_text=""):
        if not self.chibi_director.visible:
            return False
        if reply_text:
            self.chibi_director.say(reply_text[:140])
            GLib.timeout_add(1600, self._chibi_finish_now)
        else:
            self._chibi_finish_now()
        return False

    def _chibi_finish_now(self):
        self.chibi_director.finish()
        # the face comes back into the pill as the chibi flies home
        delay = int(toby_anim.animation_settings(SETTINGS)["chibi_transform_duration"] * 1000) + 700
        GLib.timeout_add(delay, self._chibi_returned)
        return False

    def _chibi_returned(self):
        if self.get_visible():
            self.face.set_state(State.IDLE)
        self.face.pulse_happy()
        return False

    def _chibi_choreograph(self, action):
        """Runs on the task thread before each step: walk there, get ready.

        Every wait is bounded, so an animation that stalls or a chibi that
        is turned off mid-task can only ever make a step start a moment
        later — never stop it from running.
        """
        if not self.chibi_director.visible:
            return
        tool = action.get("tool")
        screen = self.get_screen()
        sw, sh = screen.get_width(), screen.get_height()

        def on_main(fn, *args):
            GLib.idle_add(lambda: fn(*args) and False)

        # the bubble always names the step being done right now
        on_main(self.chibi_director.say, describe_action(action))

        if tool == "move_mouse":
            tx = float(action.get("x", 0.5)) * sw
            ty = float(action.get("y", 0.5)) * sh
            arrived = threading.Event()
            on_main(self.chibi_director.walk_to, tx, ty, arrived)
            arrived.wait(timeout=4.0)
        elif tool == "click_mouse":
            tx = screen_control.current_x_frac * sw
            ty = screen_control.current_y_frac * sh
            arrived = threading.Event()
            on_main(self.chibi_director.walk_to, tx, ty, arrived)
            arrived.wait(timeout=4.0)
            on_main(self.chibi_director.perform, "press", 0.4, "reach")
            time.sleep(0.12)   # let the tap land visibly before the click does
        elif tool in ("type_text", "key_press"):
            length = len(str(action.get("text", action.get("keys", ""))))
            on_main(self.chibi_director.perform, "type", min(3.0, 0.5 + length * 0.04))
            time.sleep(0.25)
        elif tool in ("open_url", "open_app"):
            on_main(self.chibi_director.perform, "reach", 0.6)
            time.sleep(0.3)
        else:
            on_main(self.chibi_director.perform, "think", 0.5)
            time.sleep(0.15)

    CONFIRM_TEXT = "Let Toby use your mouse and keyboard?"

    def _start_fingerprint_scan(self, attempt=1):
        """If turned on and a reader is set up, let a fingerprint say yes."""
        if not (SETTINGS.get("confirm_with_fingerprint") and self._fingerprint_ready):
            self.fingerprint_glyph.set_visible(False)
            return
        self.fingerprint_glyph.set_visible(True)
        self.fingerprint_glyph.set_state("scanning")
        if attempt == 1:
            self.confirm_label.set_text(self.confirm_label.get_text().rstrip("?")
                                        + "? Touch the reader, or press Yes.")

        def on_result(outcome):
            GLib.idle_add(self._on_fingerprint_result, outcome, attempt)

        self._fingerprint_scan = fingerprint.FingerprintScan(on_result).start()

    def _on_fingerprint_result(self, outcome, attempt):
        if not self.confirm_row.get_visible():
            return False   # answered some other way already
        if outcome == "match":
            self.fingerprint_glyph.set_state("match")
            GLib.timeout_add(250, lambda: self.on_confirm_yes() or False)
        elif outcome == "no-match" and attempt < 3:
            self.fingerprint_glyph.set_state("miss")
            GLib.timeout_add(600, lambda: self._start_fingerprint_scan(attempt + 1) or False)
        elif outcome != "cancelled":
            # out of attempts, or the reader errored: the buttons still work
            self.fingerprint_glyph.set_state("miss")
        return False

    def _cancel_fingerprint_scan(self):
        scan = getattr(self, "_fingerprint_scan", None)
        if scan is not None:
            scan.cancel()
            self._fingerprint_scan = None
        self.fingerprint_glyph.set_visible(False)

    def _check_fingerprint_reader(self):
        """Find out once, off the GTK thread, whether a reader is usable."""
        def worker():
            ready = fingerprint.reader_available()
            GLib.idle_add(lambda: setattr(self, "_fingerprint_ready", ready) or False)
        threading.Thread(target=worker, daemon=True).start()

    def _show_confirm_dialog(self):
        self._start_fingerprint_scan()
        GLib.idle_add(lambda: self._publish_remote_state(force=True))
        self.island.hide_island()
        self.set_visible(True)
        self.present()
        self.confirm_row.set_visible(True)
        self.confirm_row.show()
        self.confirm_label.set_visible(True)
        self.confirm_label.show()
        self.confirm_yes_btn.set_visible(True)
        self.confirm_yes_btn.show()
        self.confirm_no_btn.set_visible(True)
        self.confirm_no_btn.show()
        self.current.queue_resize()
        self.queue_resize()
        self.input_shape_combine_region(None)
        return False

    def finish_cancelled(self):
        self._set_task_step_status(self._current_step_index(), "error")
        self._chibi_end()
        self._busy = False
        self._remote_reply = "Cancelled."
        self._publish_remote_state(force=True)
        self.face.set_state(State.IDLE)
        self.answer.set_text("Cancelled.")
        self.answer.set_visible(True)
        self.answer.show()
        self.island.hide_island()
        self.island_expanded.close()
        return False

    def finish_response(self, text, result, actions_succeeded=0):
        self._waiting_for_first_chunk = False  # safety net in case no partial "reply" text ever streamed
        self._chibi_end(result.get("reply", "") if not self.task_cancelled else "")
        self._busy = False
        self._remote_reply = result.get("reply", "") or ("Cancelled." if self.task_cancelled else "")
        self._publish_remote_state(force=True)

        self.current_mood = result.get("mood", "neutral")
        self.face.set_state(State.RESPONDING)
        if actions_succeeded:
            self.face.pulse_happy()
        reply_text = result.get("reply", "")
        if self.task_cancelled:
            reply_text = reply_text or "Cancelled partway through."
        self.answer.set_text(reply_text)
        self.answer.set_visible(True)
        self.answer.show()
        self.thinking_content.set_visible(False)
        self._append_history_row(text, reply_text)

        if self.voice.enabled and not self.task_cancelled:
            unspoken_tail = reply_text[self._voice_spoken_len:].strip()
            if unspoken_tail:
                self.voice.speak(unspoken_tail)
        self._voice_spoken_len = 0

        self.island_expanded.close()
        if self.get_visible():
            self.island.hide_island()
        elif SETTINGS.get("notifications_enabled", True):
            self._show_reply_card(text, reply_text)

        self.history.append({"role": "user", "content": text})
        self.history.append({"role": "assistant", "content": reply_text})

        # Everything below is bookkeeping the user is not waiting on, and two
        # pieces of it (fact extraction and study-note extraction) are each a
        # fresh blocking Ollama request. finish_response runs on the GTK main
        # thread, so doing that here froze the whole UI for several seconds
        # after every single reply, right as the answer appeared. It runs on a
        # worker thread instead. Nothing in here touches a widget.
        history_snapshot = list(self.history)
        screen_context = last_screen_context
        memory_enabled = SETTINGS.get("memory_enabled", True)

        def persist_worker():
            if memory_enabled:
                try:
                    knowledge.extract_and_store(text)
                except Exception as e:
                    print("KNOWLEDGE EXTRACT ERROR:", e, flush=True)
                try:
                    study_notes.extract_and_store(text, screen_context=screen_context)
                except Exception as e:
                    print("STUDY NOTE EXTRACT ERROR:", e, flush=True)
            try:
                save_history(history_snapshot)
            except Exception as e:
                print("HISTORY SAVE ERROR:", e, flush=True)
            try:
                log_session(text, reply_text)
            except Exception as e:
                print("SESSION LOG ERROR:", e, flush=True)
            if memory_enabled:
                # the Memories graph may have just gained a node
                GLib.idle_add(self._refresh_memories_if_showing)

        threading.Thread(target=persist_worker, daemon=True).start()

        GLib.timeout_add_seconds(6, self._back_to_idle)
        return False

    # -- notification cards -----------------------------------------------------
    SNOOZE_SECONDS = 300

    def _show_reply_card(self, asked, reply_text):
        """Offer the finished reply as something the user can act on.

        Toby answers with the panel closed often enough — a voice command, a
        background task, a reply that arrived after the panel was dismissed —
        that "Toby has a reply ready" on its own was a dead end. The reply
        itself is right there now, with the four things anyone actually wants
        to do about it.
        """
        preview = " ".join(reply_text.split())
        if len(preview) > 220:
            preview = preview[:217] + "…"

        def open_panel():
            self.show_panel()
            self.answer.set_text(reply_text)
            self.answer.set_visible(True)
            self.answer.show()

        def explain():
            self.show_panel()
            self.entry.set_text("Explain that in more detail.")
            self.on_submit(self.entry)

        def later():
            GLib.timeout_add_seconds(
                self.SNOOZE_SECONDS,
                lambda: self._show_reply_card(asked, reply_text) or False)

        self.island.show_card(
            "Toby has a reply",
            preview,
            [("Open", open_panel),
             ("Explain", explain),
             ("Later", later),
             ("Dismiss", lambda: None)],
        )

    def update_streaming_answer(self, content):
        display = extract_partial_reply(content)
        if display:
            self._remote_reply = display
            self._publish_remote_state()
        if display is None:
            try:
                parsed = json.loads(content.strip().removeprefix("```json").removeprefix("```").removesuffix("```"))
                display = parsed.get("reply", content) if isinstance(parsed, dict) else content
            except (json.JSONDecodeError, AttributeError):
                display = content

        if display and display.strip() and self._waiting_for_first_chunk:
            self._waiting_for_first_chunk = False
            self.island.hide_island()  # stop showing "thinking" once real text is actually arriving

        if not self.get_visible():
            return False
        self.answer.set_text(display)
        self.answer.set_visible(True)
        self.answer.show()
        return False

    def update_thinking_raw(self, content):
        if not self.get_visible():
            return False
        self.thinking_content.set_text(content[-400:])
        self.thinking_content.set_visible(True)
        self.thinking_content.show()
        return False

    def _back_to_idle(self):
        self.face.set_state(State.IDLE)
        return False

    # -- confirm handlers -----------------------------------------------------
    def on_confirm_yes(self, *_a):
        self._cancel_fingerprint_scan()
        GLib.idle_add(lambda: self._publish_remote_state(force=True))
        screen_control.grant()
        install_guard.grant_once()
        self.confirm_row.set_visible(False)
        self.input_shape_combine_region(None)
        purpose = self._pending_confirm_purpose
        self._pending_confirm_purpose = None
        self.confirm_label.set_text(self.CONFIRM_TEXT)
        if purpose == "pinch_cursor" and not self._action_confirm_pending:
            # This prompt came from the Camera Mode switch, not from an
            # action waiting on a background thread, so nothing is blocked
            # on the event — just carry on and arm it.
            self._arm_pinch_cursor()
            return
        self._action_confirm_pending = False
        self._confirm_result = True
        self._confirm_event.set()
        if purpose == "pinch_cursor":
            self._arm_pinch_cursor()

    def on_confirm_no(self, *_a):
        self._cancel_fingerprint_scan()
        GLib.idle_add(lambda: self._publish_remote_state(force=True))
        screen_control.deny()
        self.confirm_row.set_visible(False)
        self.input_shape_combine_region(None)
        purpose = self._pending_confirm_purpose
        self._pending_confirm_purpose = None
        self.confirm_label.set_text(self.CONFIRM_TEXT)
        if purpose == "pinch_cursor":
            self.settings_pinch_cursor_switch.set_active(False)
            SETTINGS["camera_pinch_cursor"] = False
            toby_settings.save(SETTINGS)
            self._disarm_pinch_cursor("Hand pointing needs mouse control")
            if not self._action_confirm_pending:
                return
        self._action_confirm_pending = False
        self._confirm_result = False
        self._confirm_event.set()

    # -- show/hide with wake sequence ---------------------------------------
    def toggle(self, *_args):
        if self.get_visible() and not self._hiding:
            self.hide_panel()
        else:
            self.show_panel()

    def _fade_outer(self, gen, start, end, duration, curve, on_done=None):
        """Fade the pill's contents between two opacities on the frame clock.

        Opacity goes on the inner container, not the window: GDK on Wayland
        ignores opacity on a toplevel, but a child widget's opacity is
        composited by GTK itself and works everywhere. Any newer show/hide
        (a higher generation) cancels this one mid-fade, so the two never
        fight.
        """
        t0 = time.monotonic()
        finished = [False]

        def finish():
            if finished[0]:
                return
            finished[0] = True
            self.outer.set_opacity(end)
            if on_done:
                on_done()

        def step(_widget, _clock):
            if gen != self._panel_generation or finished[0]:
                return GLib.SOURCE_REMOVE
            t = (time.monotonic() - t0) / max(0.001, duration)
            self.outer.set_opacity(lerp(start, end, curve(t)))
            if t >= 1.0:
                finish()
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE

        self.outer.set_opacity(start)
        self.outer.add_tick_callback(step)

        # Frame callbacks stop when the screen locks or the display sleeps.
        # If that happens mid-fade, finish on the clock instead, so the pill
        # can never be left invisible or half-hidden. Completion is tracked
        # explicitly: a fade from 0 to 0 (hiding right after showing) is
        # still a fade that has to call on_done.
        def finish_anyway():
            if gen == self._panel_generation:
                finish()
            return False

        GLib.timeout_add(int(duration * 1000) + 250, finish_anyway)

    def show_panel(self):
        self._panel_generation += 1
        self._hiding = False
        gen = self._panel_generation
        anim = toby_anim.animation_settings(SETTINGS)
        if anim["appear_enabled"]:
            self._fade_outer(gen, 0.0, 1.0, anim["appear_duration"], toby_anim.ease_out_expo)
        else:
            self.outer.set_opacity(1.0)
        self.ring.fire()
        self.outer.get_style_context().remove_class("apple-agent-panel")
        self.outer.get_style_context().add_class("apple-agent-panel-hidden")

        start_margin = -300  # fully below the visible screen, slides up from here
        end_margin = 55
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.BOTTOM, start_margin)

        self.set_visible(True)
        self.present()
        self.set_can_focus(True)
        self.grab_focus()
        self.face.set_state(State.WAKING)
        self.last_interaction = time.monotonic()

        slide_state = {"i": 0}
        slide_steps = 16  # fewer steps, same 16ms cadence -> a quick ~250ms snap instead of ~400ms

        def slide_step():
            if gen != self._panel_generation:
                return False  # a hide (or another show) happened mid-animation — stop
            slide_state["i"] += 1
            t = min(1.0, slide_state["i"] / slide_steps)
            eased = ease_out_back(t)  # slight overshoot for a snappy, springy feel
            margin = int(start_margin + (end_margin - start_margin) * eased)
            GtkLayerShell.set_margin(self, GtkLayerShell.Edge.BOTTOM, margin)
            return t < 1.0

        GLib.timeout_add(16, slide_step)
        GLib.timeout_add(220, lambda: self._reveal_box(gen))
        GLib.timeout_add(300, lambda: self._reveal_entry(gen))

    def _reveal_box(self, gen):
        if gen != self._panel_generation:
            return False
        self.outer.get_style_context().remove_class("apple-agent-panel-hidden")
        self.outer.get_style_context().add_class("apple-agent-panel")
        return False

    def _reveal_entry(self, gen):
        if gen != self._panel_generation:
            return False
        self.entry.set_visible(True)
        self.close_btn.set_visible(True)
        self.mic_btn.set_visible(True)
        self.camera_btn.set_visible(True)
        self.expand_btn.set_visible(True)
        self.smart_btn.set_visible(True)
        self.entry.set_sensitive(True)
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.ON_DEMAND)
        self.entry.grab_focus()
        if self.face.state == State.WAKING:
            # Only settle into idle if nothing has happened since. A voice
            # command opens the panel and submits in the same breath, so by
            # now the face is often already thinking — forcing idle here
            # dropped it out of the thinking animation a third of a second in.
            self.face.set_state(State.IDLE)
        self.input_shape_combine_region(None)
        return False

    def hide_panel(self):
        self._panel_generation += 1  # invalidate any in-flight show animation immediately
        gen = self._panel_generation
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)
        anim = toby_anim.animation_settings(SETTINGS)
        if anim["appear_enabled"] and self.get_visible():
            self._hiding = True
            # fade out while sinking a little, the reverse of arriving
            self.face.set_state(State.SLEEPING)
            start_margin = GtkLayerShell.get_margin(self, GtkLayerShell.Edge.BOTTOM)
            t0 = time.monotonic()
            duration = anim["disappear_duration"]

            def sink():
                if gen != self._panel_generation:
                    return False
                t = (time.monotonic() - t0) / duration
                GtkLayerShell.set_margin(self, GtkLayerShell.Edge.BOTTOM,
                                         int(start_margin - 26 * toby_anim.ease_in_cubic(t)))
                return t < 1.0

            GLib.timeout_add(16, sink)
            self._fade_outer(gen, self.outer.get_opacity(), 0.0, duration,
                             toby_anim.ease_in_cubic, on_done=lambda: self._finish_hide(gen))
            return
        self._finish_hide(gen)

    def _finish_hide(self, gen):
        if gen != self._panel_generation:
            return  # shown again while fading out; leave it be
        self._hiding = False
        self.outer.set_opacity(1.0)
        self.set_visible(False)
        self.entry.set_visible(False)
        self.close_btn.set_visible(False)
        self.mic_btn.set_visible(False)
        self.camera_btn.set_visible(False)
        self.expand_btn.set_visible(False)
        self.smart_btn.set_visible(False)
        self.face.set_state(State.SLEEPING)


def main():
    apply_model_settings()
    warm_up_model()
    ring = RingFlash()
    win = AssistantWindow(ring)

    def handle_usr1(signum, frame):
        GLib.idle_add(win.toggle)

    import signal
    signal.signal(signal.SIGUSR1, handle_usr1)
    GLib.timeout_add(200, lambda: True)  # lets Python signal handlers run promptly

    Gtk.main()


if __name__ == "__main__":
    main()
