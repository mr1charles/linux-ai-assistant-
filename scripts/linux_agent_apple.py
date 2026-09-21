#!/usr/bin/env python3
"""
Little Toby — an animated, local-first Linux desktop AI assistant.

A rainbow ring flash, a small animated chibi face that wakes up, and a
sliding textbox — all custom-drawn with Cairo, no external animation
libraries. Talks to a local Ollama model, keeps a personal knowledge graph
and study notes, can (with explicit per-session confirmation) control the
mouse/keyboard and install packages, and can shift into Study Mode on a
schedule or automatically when it recognizes schoolwork on screen.

Setup: see README.md in the project root.

Hyprland keybind (Super+G to summon):
  bind = SUPER, G, exec, pkill -SIGUSR1 -f linux_agent_apple.py
  exec-once = bash -c 'set -a; source ~/linux-agent/.env; set +a; python3 ~/linux-agent/scripts/linux_agent_apple.py'

For real background blur behind the panel (glassmorphism), Hyprland can blur
by namespace — add to hyprland.conf:
  layerrule = blur, apple-agent
  layerrule = ignorezero, apple-agent
"""

import json
import math
import os
import re
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

SETTINGS = toby_settings.load()

REPO_DIR = Path.home() / "linux-agent"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/chat")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b-instruct")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
EMAIL_ADDRESS = os.environ.get("EMAIL_ADDRESS")
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD")
IMAP_SERVER = os.environ.get("IMAP_SERVER", "imap.gmail.com")

HISTORY_PATH = REPO_DIR / "history.json"
SESSION_LOG_PATH = REPO_DIR / "session_log.md"

FPS_MS = 16  # ~60fps tick
IDLE_TIMEOUT_S = 25  # how long with no interaction before it "falls asleep"

SYSTEM_PROMPT_TEMPLATE = """Today's date is {today}. You are Little Toby, a task-doing desktop \
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

{context_block}

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
    text = m.group(1)
    return text.replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\')


def _build_system_content():
    today = datetime.date.today().strftime("%B %d, %Y")
    facts_context = knowledge.get_facts_summary()
    notes_context = study_notes.get_notes_summary()
    active_window = get_active_window_context()
    context_lines = []
    if active_window:
        context_lines.append(f"Currently focused window: {active_window}")
    if recent_actions:
        recent_str = "; ".join(f"{a['tool']} ({a['summary']})" for a in recent_actions)
        context_lines.append(f"Actions taken earlier this session (most recent last): {recent_str}")
    context_block = ("Ambient context (for your awareness, not something the user necessarily "
                      "mentioned):\n" + "\n".join(context_lines)) if context_lines else ""
    extra_context = "\n\n".join(c for c in [facts_context, notes_context] if c)
    system_content = SYSTEM_PROMPT_TEMPLATE.format(today=today, context_block=context_block)
    system_content += "\n\n" + toby_settings.style_prompt_line(SETTINGS)
    grade = SETTINGS.get("grade_level", "9th-10th")
    system_content += (f"\n\nWhen explaining or summarizing academic material, pitch it at a "
                        f"{grade} level of complexity.")
    system_content += ("\n\n" + extra_context if extra_context else "")
    return system_content


def think(instruction, history, on_chunk=None, cancel_check=None):
    system_content = _build_system_content()
    messages = [{"role": "system", "content": system_content}]
    messages.extend(history[-12:])
    messages.append({"role": "user", "content": instruction})
    model = SETTINGS.get("ollama_model") or OLLAMA_MODEL
    payload = {"model": model, "messages": messages, "stream": bool(on_chunk), "format": "json"}

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
    messages.extend(history[-12:])
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
    SIZE = 40

    def __init__(self):
        super().__init__()
        self.set_size_request(self.SIZE, self.SIZE)
        self.connect("draw", self.on_draw)

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

    def set_audio_level(self, level):
        """0..1 mic input level, fed continuously while Voice Mode is listening —
        makes the face genuinely react to how loud you're talking, not a canned loop."""
        self.audio_level = max(0.0, min(1.0, level))

    def set_speaking(self, speaking):
        self._speaking = speaking

    def tick(self, mood="neutral"):
        now = time.monotonic()
        elapsed_in_state = now - self.state_since

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

        self.breath = math.sin(now * 1.6) * 1.5
        self.bob = math.sin(now * 1.1) * 2.0 if self.state != State.SLEEPING else 0.0
        self.sway = math.sin(now * 0.65) * 1.2 if self.state not in (State.SLEEPING, State.WAKING) else 0.0
        self.tilt = math.sin(now * 0.5) * 0.035 if self.state == State.IDLE else lerp(self.tilt, 0.0, 0.2)

        self.queue_draw()

    def on_draw(self, widget, cr):
        w, h = self.get_allocated_width(), self.get_allocated_height()
        cx, cy = w / 2 + self.sway, h / 2 + self.bob
        r = (min(w, h) / 2 - 3 + self.breath * 0.3) * max(0.06, self.face_scale)

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
        box.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        inner.set_border_width(10)
        box.add(inner)

        self.dot = Gtk.DrawingArea()
        self.dot.set_size_request(10, 10)
        self.dot.connect("draw", self._draw_dot)
        inner.pack_start(self.dot, False, False, 0)

        self.label = Gtk.Label(label="")
        self.label.get_style_context().add_class("apple-agent-island-label")
        inner.pack_start(self.label, False, False, 0)

        box.connect("button-press-event", self._on_button_press)
        self.add(box)

        self.show_all()
        self.set_visible(False)
        self._pulse_t0 = time.monotonic()
        GLib.timeout_add(50, self._pulse_tick)

    def _on_button_press(self, widget, event):
        if event.type == Gdk.EventType._2BUTTON_PRESS:
            if self._click_timeout_id:
                GLib.source_remove(self._click_timeout_id)
                self._click_timeout_id = None
            self._on_expand()
        elif event.type == Gdk.EventType.BUTTON_PRESS:
            # debounce: wait briefly to see if this becomes a double-click before
            # treating it as a plain single click
            if self._click_timeout_id:
                GLib.source_remove(self._click_timeout_id)

            def fire_single():
                self._click_timeout_id = None
                self._on_click()
                return False

            self._click_timeout_id = GLib.timeout_add(200, fire_single)
        return False

    def _draw_dot(self, widget, cr):
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        pulse = 0.6 + 0.4 * math.sin((time.monotonic() - self._pulse_t0) * 4.0)
        cr.set_source_rgba(0.45, 0.75, 1.0, pulse)
        cr.arc(w / 2, h / 2, min(w, h) / 2, 0, 2 * math.pi)
        cr.fill()
        return False

    def _pulse_tick(self):
        if self.get_visible():
            self.dot.queue_draw()
        return True

    def set_status(self, text):
        self.label.set_text(text)

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

CSS = b"""
.apple-agent-panel {
    background-color: rgba(26, 26, 30, 0.74);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 50px;
    box-shadow: 0 10px 40px rgba(0,0,0,0.4), 0 1px 0 rgba(255,255,255,0.05) inset;
}
.apple-agent-sidebar-window {
    background-color: rgba(18, 18, 22, 0.97);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 18px;
    box-shadow: 0 20px 60px rgba(0,0,0,0.6), 0 1px 0 rgba(255,255,255,0.05) inset;
}
.apple-agent-panel-hidden {
    background-color: rgba(0, 0, 0, 0);
    border-radius: 50px;
    box-shadow: none;
}
.apple-agent-entry {
    background: transparent;
    color: #f5f5f7;
    border: none;
    font-size: 15px;
    caret-color: #f5f5f7;
    box-shadow: 0 0 8px rgba(150, 150, 255, 0.25);
    transition: box-shadow 150ms ease-out;
}
.apple-agent-entry:focus {
    outline: none;
    box-shadow: 0 0 18px rgba(150, 180, 255, 0.55), 0 0 6px rgba(255, 150, 200, 0.35);
}
.apple-agent-face-hit-target {
    background-color: transparent;
    background-image: none;
    border: none;
    box-shadow: none;
}
.apple-agent-close {
    color: rgba(245,245,247,0.55);
    font-size: 13px;
    min-width: 20px;
    min-height: 20px;
    padding: 0;
    border-radius: 50%;
    background: transparent;
    transition: background 120ms ease-out, color 120ms ease-out;
}
.apple-agent-close:hover {
    color: #f5f5f7;
    background: rgba(255,255,255,0.16);
}
.apple-agent-close:active {
    background: rgba(255,255,255,0.28);
}
.apple-agent-answer {
    color: #f5f5f7;
    font-size: 14px;
}
.apple-agent-thinking {
    color: rgba(245,245,247,0.4);
    font-size: 11px;
    font-family: monospace;
}
.apple-agent-history-user {
    color: rgba(245,245,247,0.55);
    font-size: 13px;
}
.apple-agent-history-reply {
    color: #f5f5f7;
    font-size: 13px;
}
.apple-agent-badge {
    color: #f5f5f7;
    background: rgba(90, 140, 255, 0.35);
    border-radius: 12px;
    padding: 2px 10px;
    font-size: 12px;
    transition: background 120ms ease-out;
}
.apple-agent-badge:hover {
    background: rgba(90, 140, 255, 0.5);
}
button.apple-agent-panel-button {
    color: rgba(245,245,247,0.75);
    font-size: 12px;
    padding: 3px 10px;
    border-radius: 10px;
    background: rgba(255,255,255,0.06);
    transition: background 120ms ease-out, color 120ms ease-out;
}
button.apple-agent-panel-button:hover {
    color: #f5f5f7;
    background: rgba(255,255,255,0.14);
}
.apple-agent-island {
    background-color: rgba(20, 20, 24, 0.88);
    border-radius: 22px;
    border: 1px solid rgba(255,255,255,0.08);
    box-shadow: 0 6px 24px rgba(0,0,0,0.45);
}
.apple-agent-island-label {
    color: #f5f5f7;
    font-size: 12px;
}
.apple-agent-island-expanded {
    background-color: rgba(20, 20, 24, 0.92);
    border-radius: 20px;
    border: 1px solid rgba(255,255,255,0.08);
    box-shadow: 0 10px 32px rgba(0,0,0,0.5);
}
.apple-agent-island-task {
    color: #f5f5f7;
    font-size: 13px;
    font-weight: bold;
}
.apple-agent-island-elapsed {
    color: rgba(245,245,247,0.5);
    font-size: 11px;
}
.apple-agent-step-done {
    color: rgba(120, 220, 150, 0.9);
    font-size: 12px;
}
.apple-agent-step-current {
    color: #f5f5f7;
    font-size: 12px;
}
.apple-agent-step-pending {
    color: rgba(245,245,247,0.35);
    font-size: 12px;
}
.apple-agent-step-error {
    color: rgba(240, 110, 110, 0.9);
    font-size: 12px;
}
.apple-agent-nav-sidebar {
    background: rgba(15, 15, 18, 0.95);
    border-right: 2px solid rgba(90, 140, 255, 0.35);
}
.apple-agent-nav-title {
    color: rgba(245,245,247,0.4);
    font-size: 10px;
    font-weight: bold;
    letter-spacing: 1px;
}
button.apple-agent-nav-button {
    color: rgba(245,245,247,0.85);
    font-size: 14px;
    padding: 10px 14px;
    border-radius: 8px;
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.08);
    transition: background 120ms ease-out, color 120ms ease-out;
}
button.apple-agent-nav-button:hover {
    color: #ffffff;
    background: rgba(255,255,255,0.16);
    border: 1px solid rgba(255,255,255,0.2);
}
button.apple-agent-nav-button.nav-selected {
    color: #ffffff;
    background: rgba(90, 140, 255, 0.55);
    border: 1px solid rgba(120, 160, 255, 0.8);
    font-weight: bold;
}
.apple-agent-dashboard-title {
    color: rgba(245,245,247,0.55);
    font-size: 12px;
}
.apple-agent-dashboard-value {
    color: #f5f5f7;
    font-size: 13px;
    font-weight: bold;
}
.apple-agent-study-helper {
    background-color: rgba(20, 20, 24, 0.9);
    border-radius: 18px;
    border: 1px solid rgba(255,255,255,0.08);
    box-shadow: 0 8px 28px rgba(0,0,0,0.45);
}
button.apple-agent-topic-card {
    color: #ffffff;
    font-size: 12px;
    padding: 10px;
    border-radius: 10px;
    min-width: 130px;
    min-height: 60px;
}
button.apple-agent-topic-none {
    background: rgba(255,255,255,0.08);
    border: 1px solid rgba(255,255,255,0.15);
}
button.apple-agent-topic-low {
    background: rgba(220, 80, 80, 0.45);
    border: 1px solid rgba(255, 110, 110, 0.6);
}
button.apple-agent-topic-mid {
    background: rgba(220, 180, 60, 0.45);
    border: 1px solid rgba(255, 210, 90, 0.6);
}
button.apple-agent-topic-high {
    background: rgba(80, 200, 120, 0.45);
    border: 1px solid rgba(110, 230, 150, 0.6);
}
"""


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
        for i, level in enumerate(self.levels):
            bar_h = max(2, level * h)
            x = i * bar_w
            cr.set_source_rgba(0.45, 0.75, 1.0, 0.35 + 0.5 * level)
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
        hue = (hash(category) % 360) / 360.0
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
            cr.set_source_rgba(1, 1, 1, 0.4)
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
                cr.set_source_rgba(1, 1, 1, 0.9)
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

        self.school_mode_config = school_mode.load_config()
        self.school_scheduled_active = False

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

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        screen_control.get_screen_size = lambda: (screen.get_width(), screen.get_height())

        self.outer = Gtk.EventBox()
        self.outer.get_style_context().add_class("apple-agent-panel-hidden")
        self.add(self.outer)

        self.stage = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.stage.set_border_width(8)
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
        confirm_label = Gtk.Label(label="Control screen enable?")
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
        self.sidebar_window.add(sidebar_outer)

        self.expanded_area = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        sidebar_outer.add(self.expanded_area)

        self.nav_sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.nav_sidebar.set_size_request(170, -1)
        self.nav_sidebar.set_border_width(10)
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
        dashboard_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        dashboard_page.set_border_width(14)
        self.dashboard_labels = {}
        for stat_key, stat_title in [
            ("status", "AI Status"), ("task", "Current Task"), ("cpu", "CPU"),
            ("mem", "Memory"), ("context", "Context Window"), ("tools", "Active Tools"),
            ("automations", "Automations"), ("today", "Messages Today"),
        ]:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            title_lbl = Gtk.Label(label=stat_title)
            title_lbl.set_xalign(0)
            title_lbl.get_style_context().add_class("apple-agent-dashboard-title")
            title_lbl.set_size_request(150, -1)
            row.pack_start(title_lbl, False, False, 0)
            value_lbl = Gtk.Label(label="—")
            value_lbl.set_xalign(0)
            value_lbl.get_style_context().add_class("apple-agent-dashboard-value")
            row.pack_start(value_lbl, True, True, 0)
            dashboard_page.pack_start(row, False, False, 0)
            self.dashboard_labels[stat_key] = value_lbl
        self.content_stack.add_titled(dashboard_page, "dashboard", "Dashboard")

        # --- Chat page (conversation history) --------------------------------
        chat_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        chat_page.set_border_width(8)
        self.history_scroller = Gtk.ScrolledWindow()
        self.history_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.history_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.history_scroller.add(self.history_box)
        chat_page.pack_start(self.history_scroller, True, True, 0)
        self.content_stack.add_titled(chat_page, "chat", "Chat")

        # --- Memories page (interactive graph) --------------------------------
        memories_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        memories_page.set_border_width(8)
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
        export_tree_btn.connect("clicked", lambda *_: knowledge.render_tree())
        export_row.pack_start(export_tree_btn, False, False, 0)
        export_bubbles_btn = Gtk.Button(label="Export as Bubble Image")
        export_bubbles_btn.get_style_context().add_class("apple-agent-panel-button")
        export_bubbles_btn.connect("clicked", lambda *_: knowledge.render_bubbles())
        export_row.pack_start(export_bubbles_btn, False, False, 0)
        memories_page.pack_start(export_row, False, False, 0)

        self.content_stack.add_titled(memories_page, "memories", "Memories")

        # --- Study Plan page (proficiency grid) ---------------------------------
        plan_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        plan_page.set_border_width(10)

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
        settings_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        settings_page.set_border_width(14)

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
        self.settings_model_entry.set_placeholder_text(f"blank = {OLLAMA_MODEL}")
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
        voice_separator.get_style_context().add_class("apple-agent-island-task")
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
        camera_separator.get_style_context().add_class("apple-agent-island-task")
        settings_page.pack_start(camera_separator, False, False, 6)

        camera_switch_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        camera_switch_row.pack_start(Gtk.Label(label="Camera Mode enabled"), False, False, 0)
        self.settings_camera_switch = Gtk.Switch()
        self.settings_camera_switch.set_active(SETTINGS.get("camera_mode_enabled", False))
        self.settings_camera_switch.connect("state-set", self._on_camera_switch_toggled)
        camera_switch_row.pack_end(self.settings_camera_switch, False, False, 0)
        settings_page.pack_start(camera_switch_row, False, False, 0)

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

        cloud_separator = Gtk.Label(label="Cloud AI (optional)")
        cloud_separator.set_xalign(0)
        cloud_separator.get_style_context().add_class("apple-agent-island-task")
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
        settings_save_btn.get_style_context().add_class("apple-agent-panel-button")
        settings_save_btn.connect("clicked", self.on_settings_save_clicked)
        settings_page.pack_start(settings_save_btn, False, False, 0)

        self.content_stack.add_titled(settings_page, "settings", "Settings")

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
        self.camera_overlay = CameraOverlay()
        self._camera_recording_name = None
        self._pending_camera_action_for_record = None
        self.refresh_camera_gesture_list()
        if SETTINGS.get("camera_mode_enabled", False):
            self.camera.start()
            self.camera_overlay.set_visible(True)

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

    # -- animation driver --------------------------------------------------
    def tick(self):
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
        self.dashboard_labels["context"].set_text(f"{min(len(self.history), 12)}/12 messages")
        self.dashboard_labels["tools"].set_text(str(len(DISPATCH)))
        automations = []
        if study_mode_state.active:
            automations.append("Study Mode ON")
        if self.school_scheduled_active:
            automations.append("School schedule active")
        self.dashboard_labels["automations"].set_text(", ".join(automations) if automations else "None")
        self.dashboard_labels["today"].set_text(str(count_todays_sessions()))
        return True  # keep repeating on the periodic timer

    # -- Memories ---------------------------------------------------------------
    def refresh_memory_graph(self):
        data = knowledge.get_graph_data()
        self.memory_graph.load_data(data["nodes"], data["edges"])

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

        toby_settings.save(SETTINGS)
        self.settings_save_status.set_text("Saved.")
        GLib.timeout_add_seconds(3, lambda: self.settings_save_status.set_text("") or False)

    def apply_accent_color(self, hex_color):
        if not hasattr(self, "_accent_provider"):
            self._accent_provider = Gtk.CssProvider()
            Gtk.StyleContext.add_provider_for_screen(
                self.get_screen(), self._accent_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1
            )
        try:
            h = hex_color.lstrip("#")
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        except (ValueError, IndexError):
            return  # malformed color — leave the current accent alone rather than crash
        css = (
            f"button.apple-agent-nav-button.nav-selected {{ background: rgba({r},{g},{b},0.27); color: #ffffff; }}\n"
            f".apple-agent-badge {{ background: rgba({r},{g},{b},0.33); }}\n"
        ).encode()
        self._accent_provider.load_from_data(css)

    def _append_history_row(self, user_text, reply_text):
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
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
        try:
            text = capture_screen_text()
            keywords = self.school_mode_config.get("adaptive_keywords", [])
            if school_mode.screen_matches_keywords(text, keywords):
                study_mode_state.enable()
        except Exception as e:
            print("SCHOOL MODE ADAPTIVE CHECK ERROR:", e, flush=True)
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
        self.smart_mode_active = widget.get_active()
        if self.smart_mode_active and not SETTINGS.get("cloud_api_key", "").strip():
            self.answer.set_text("Smart mode needs an API key set in Settings first.")
            self.answer.set_visible(True)
            self.answer.show()

    def on_camera_button_clicked(self, *_a):
        new_state = not SETTINGS.get("camera_mode_enabled", False)
        SETTINGS["camera_mode_enabled"] = new_state
        toby_settings.save(SETTINGS)
        self.settings_camera_switch.set_active(new_state)
        if new_state:
            self.camera.start()
            self.camera_overlay.set_visible(True)
        else:
            self.camera.stop()
            self.camera_overlay.set_visible(False)

    def _on_camera_switch_toggled(self, widget, state):
        SETTINGS["camera_mode_enabled"] = state
        toby_settings.save(SETTINGS)
        if state:
            self.camera.start()
            self.camera_overlay.set_visible(True)
        else:
            self.camera.stop()
            self.camera_overlay.set_visible(False)
        return False

    def on_camera_hand_frame(self, hands_xy):
        GLib.idle_add(self.camera_overlay.set_hands, hands_xy)

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
                result_summary = fn(action)
                note_recent_action(action.get("tool", "?"), str(result_summary)[:80])
                actions_succeeded += 1
                GLib.idle_add(self._set_task_step_status, i + 1, "done")
                if action.get("tool") == "set_school_schedule":
                    self.school_mode_config = school_mode.load_config()
            except NeedsConfirmation:
                self._confirm_event.clear()
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
        self.task_steps += [(a.get("tool", "?"), "pending") for a in actions]
        if self.island_expanded.get_visible():
            self.island_expanded.set_task(self.task_label_text, self.task_steps)
        return False

    def _show_confirm_dialog(self):
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
        self.face.set_state(State.IDLE)
        self.answer.set_text("Cancelled.")
        self.answer.set_visible(True)
        self.answer.show()
        self.island.hide_island()
        self.island_expanded.close()
        return False

    def finish_response(self, text, result, actions_succeeded=0):
        self._waiting_for_first_chunk = False  # safety net in case no partial "reply" text ever streamed

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
            self.island.show_island("Toby has a reply ready")
            GLib.timeout_add_seconds(5, self.island.hide_island)

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

    def update_streaming_answer(self, content):
        display = extract_partial_reply(content)
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
        screen_control.grant()
        install_guard.grant_once()
        self.confirm_row.set_visible(False)
        self.input_shape_combine_region(None)
        self._confirm_result = True
        self._confirm_event.set()

    def on_confirm_no(self, *_a):
        screen_control.deny()
        self.confirm_row.set_visible(False)
        self.input_shape_combine_region(None)
        self._confirm_result = False
        self._confirm_event.set()

    # -- show/hide with wake sequence ---------------------------------------
    def toggle(self, *_args):
        if self.get_visible():
            self.hide_panel()
        else:
            self.show_panel()

    def show_panel(self):
        self._panel_generation += 1
        gen = self._panel_generation
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
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)
        self.set_visible(False)
        self.entry.set_visible(False)
        self.close_btn.set_visible(False)
        self.mic_btn.set_visible(False)
        self.camera_btn.set_visible(False)
        self.expand_btn.set_visible(False)
        self.smart_btn.set_visible(False)
        self.face.set_state(State.SLEEPING)


def main():
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
