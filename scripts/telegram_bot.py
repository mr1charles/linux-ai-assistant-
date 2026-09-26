#!/usr/bin/env python3
"""
telegram_bot.py — talk to Little Toby from Telegram, from anywhere.

A private Telegram bot, only yours, that passes what you say to Toby on your
computer and brings back what happens: the task's steps as they go by, any
"Toby wants to do this. Allow?" question as Allow and Don't allow buttons,
the reply, and notifications (a build finished, the computer is going to
sleep). Nothing to install on the phone beyond Telegram itself.

How it fits together
--------------------
This runs as its own small service (toby-telegram.service). It is a paired
device like the iPhone app: it talks to Toby's phone connection on
127.0.0.1 with its own token, so everything goes through the same
permission levels and approval queue, and `toby telegram off` (or `toby
phone revoke`) cuts it off. It only ever makes outgoing connections, to
Telegram's servers; nothing on the computer listens to the internet.

Who can use it
--------------
Only Telegram accounts you paired, at the computer, with `toby telegram
pair`: it shows a one-time code, you send it to the bot, and the terminal
asks whether the account that sent it is yours. Messages from anyone else
are ignored without a reply.

Privacy
-------
Telegram bot chats are not end-to-end encrypted: Telegram's servers carry
them. That's the price of not needing an app. Screenshots are never sent
unless you turn on "telegram_screenshots" (and Screen view) yourself. For a
fully private connection, use the iPhone or web app over Tailscale.
"""

import html
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

CONFIG_PATH = Path.home() / "linux-agent" / "telegram.json"
API_BASE = "https://api.telegram.org"
MAX_MESSAGE = 4096
EDIT_EVERY_S = 1.5          # Telegram limits edits; a step list doesn't need more
POLL_TIMEOUT_S = 25
PAIR_TTL_S = 300
FORWARDED_EVENTS = ("job_done", "job_failed", "power", "device")
FINISHED = ("completed", "failed", "cancelled")

STATE_WORDS = {
    "thinking": "Thinking", "preparing": "Getting ready", "working": "Working",
    "waiting": "Waiting on a command", "needs_permission": "Waiting for your OK",
    "paused": "Paused", "completed": "Done", "failed": "Couldn't finish", "cancelled": "Stopped",
}
STEP_MARKS = {"done": "✓", "current": "▸", "error": "✗", "skipped": "–"}

HELP = (
    "Talk to me like you would on the computer: \"is my game still running?\", "
    "\"run the tests in ~/project\", \"how's the build going?\". Anything that changes "
    "something asks you first, here and on the computer.\n\n"
    "/status  the computer right now\n"
    "/tasks  what you've asked lately\n"
    "/jobs  commands running in the background\n"
    "/pause  /resume  /stop  the task in progress\n"
    "/screen  a screenshot, if you've allowed it\n"
    "/workmode on|off\n"
    "/help  this"
)


# ---------------------------------------------------------------------------
# Settings kept on disk
# ---------------------------------------------------------------------------

def load_config(path=None):
    path = Path(path or CONFIG_PATH)
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(config, path=None):
    """Written readable by you alone: it holds the bot's and Toby's tokens."""
    path = Path(path or CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def allowed_ids(config):
    return {int(u["id"]) for u in config.get("allowed_users", []) if str(u.get("id", "")).lstrip("-").isdigit()}


def describe_user(user):
    name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p) or "Someone"
    if user.get("username"):
        name += f" (@{user['username']})"
    return name


# ---------------------------------------------------------------------------
# Telegram's Bot API
# ---------------------------------------------------------------------------

class TelegramError(Exception):
    def __init__(self, status, description, retry_after=None):
        super().__init__(description)
        self.status = status
        self.description = description
        self.retry_after = retry_after


class TelegramAPI:
    """The few Bot API calls Toby needs, over HTTPS with the standard library."""

    def __init__(self, token, base=API_BASE):
        self.token = token
        self.base = base.rstrip("/")

    def call(self, method, params=None, files=None, timeout=40):
        url = f"{self.base}/bot{self.token}/{method}"
        if files:
            body, ctype = _multipart(params or {}, files)
        else:
            body, ctype = json.dumps(params or {}).encode(), "application/json"
        req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                data = json.loads(e.read() or b"{}")
            except ValueError:
                data = {}
            raise TelegramError(e.code, data.get("description") or str(e),
                                (data.get("parameters") or {}).get("retry_after")) from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise TelegramError(0, f"couldn't reach Telegram ({getattr(e, 'reason', e)})") from None
        if not data.get("ok"):
            raise TelegramError(200, data.get("description", "Telegram said no"),
                                (data.get("parameters") or {}).get("retry_after"))
        return data.get("result")

    def download(self, file_path, timeout=40):
        url = f"{self.base}/file/bot{self.token}/{urllib.parse.quote(file_path)}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return resp.read(20 * 1024 * 1024)
        except (urllib.error.URLError, OSError) as e:
            raise TelegramError(0, f"couldn't download the file ({e})") from None


def _multipart(fields, files):
    boundary = "toby-" + secrets.token_hex(12)
    parts = []
    for key, value in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode())
    for key, (filename, content, ctype) in files.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"; "
                     f"filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n".encode() + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------------------
# Toby, through its phone connection on this computer
# ---------------------------------------------------------------------------

class TobyUnavailable(Exception):
    """Toby can't be asked right now; the message says why, for a person."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class TobyAPI:
    def __init__(self, token, port=8765, host="127.0.0.1"):
        self.token = token
        self.base = f"http://{host}:{port}"

    def request(self, method, path, body=None, params=None, timeout=10, raw=False):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json"})
        # Toby is on this computer: never send its requests through a proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=timeout) as resp:
                payload = resp.read()
                return resp.status, (payload if raw else json.loads(payload or b"{}"))
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read() or b"{}")
            except ValueError:
                payload = {}
            if e.code == 401:
                raise TobyUnavailable("unpaired", "This Telegram bot isn't paired with Toby any more. "
                                                  "On the computer, run: toby telegram setup") from None
            if e.code == 429:
                raise TobyUnavailable("locked", "Too many wrong attempts; Toby is ignoring this for a minute.") from None
            return e.code, payload
        except (urllib.error.URLError, OSError, ValueError):
            raise TobyUnavailable("not_running", "Toby isn't running on the computer right now "
                                                 "(or it's asleep). On the computer: toby start") from None

    def get(self, path, params=None, timeout=10, raw=False):
        return self.request("GET", path, params=params, timeout=timeout, raw=raw)

    def post(self, path, body=None, timeout=10):
        return self.request("POST", path, body=body or {}, timeout=timeout)


# ---------------------------------------------------------------------------
# Voice notes
# ---------------------------------------------------------------------------

class Transcriber:
    """Turns a Telegram voice note into text on this computer, with the same
    offline speech model as Voice Mode. Nothing is sent anywhere to do it."""

    def __init__(self, model_path=None):
        self.model_path = model_path or os.environ.get(
            "VOSK_MODEL_PATH", str(Path.home() / "linux-agent" / "vosk-model-small-en-us-0.15"))
        self._model = None

    def unavailable(self):
        """Why voice notes can't be understood here, or None if they can."""
        if not shutil.which("ffmpeg"):
            return "I can't listen to voice notes on this computer yet: it needs ffmpeg (sudo pacman -S ffmpeg)."
        try:
            import vosk  # noqa: F401
        except ImportError:
            return "I can't listen to voice notes: the speech package isn't installed. Run: toby update"
        if not os.path.isdir(self.model_path):
            return "I can't listen to voice notes: the speech model isn't downloaded. Run: toby update"
        return None

    def __call__(self, audio):
        from vosk import KaldiRecognizer, Model
        pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-ar", "16000", "-ac", "1",
                              "-f", "s16le", "pipe:1"], input=audio, capture_output=True, timeout=60).stdout
        if self._model is None:
            self._model = Model(self.model_path)
        rec = KaldiRecognizer(self._model, 16000)
        for i in range(0, len(pcm), 8000):
            rec.AcceptWaveform(pcm[i:i + 8000])
        return json.loads(rec.FinalResult()).get("text", "").strip()


# ---------------------------------------------------------------------------
# The bot
# ---------------------------------------------------------------------------

def chunks(text, limit=MAX_MESSAGE - 96):
    """Split a long reply into messages Telegram accepts, at line breaks."""
    text = text or ""
    out, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                out.append(current)
                current = ""
            out.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            out.append(current)
            current = ""
        current += line
    if current.strip() or not out:
        out.append(current)
    return out


def render_task(task):
    """The live status message for a task: what you asked, its state, its steps."""
    state = task.get("state") or "thinking"
    lines = [f"<b>{html.escape(task.get('text', '')[:200])}</b>", STATE_WORDS.get(state, state.title())]
    for step in task.get("steps") or []:
        mark = STEP_MARKS.get(step.get("status"), "·")
        line = f"{mark} {html.escape(str(step.get('label', ''))[:120])}"
        detail = str(step.get("detail") or "").strip()
        if detail and step.get("status") in ("done", "error"):
            line += f"\n    <i>{html.escape(detail[:160])}</i>"
        lines.append(line)
    return "\n".join(lines)


def render_status(status):
    parts = []
    computer = (status.get("computer") or {}).get("name") or "Your computer"
    parts.append(f"<b>{html.escape(computer)}</b>")
    cpu = status.get("cpu_percent")
    if cpu is not None:
        parts.append(f"CPU {cpu:.0f}%")
    mem = status.get("memory") or {}
    if mem.get("percent") is not None:
        parts.append(f"Memory {mem['percent']:.0f}% used")
    battery = status.get("battery") or {}
    if battery.get("percent") is not None:
        parts.append(f"Battery {battery['percent']:.0f}%" + (", charging" if battery.get("charging") else ""))
    if status.get("uptime_s"):
        hours = int(status["uptime_s"] // 3600)
        parts.append(f"Up {hours // 24} d {hours % 24} h" if hours >= 24 else f"Up {hours} h {int(status['uptime_s'] % 3600 // 60)} min")
    toby = status.get("toby") or {}
    if toby.get("current_task"):
        parts.append(f"Toby is working on: {html.escape(toby['current_task'][:120])}")
    else:
        parts.append("Toby is free")
    if status.get("jobs_running"):
        parts.append(f"{status['jobs_running']} command(s) running in the background")
    return "\n".join(parts)


class TelegramBot:
    def __init__(self, telegram, toby, config_path=None, settings=lambda: {}, transcriber=None,
                 clock=time.monotonic):
        self.tg = telegram
        self.toby = toby
        self.config_path = config_path
        self.config = load_config(config_path)
        self.settings = settings
        self.transcriber = transcriber if transcriber is not None else Transcriber()
        self.clock = clock
        self.stop_event = threading.Event()
        self._lock = threading.RLock()
        self.active = None          # the task you asked for here: {task_id, chat_id, message_id, text, shown, at}
        self.approval_messages = {}  # approval id -> [(chat_id, message_id, title)]
        self.answered = {}           # approval id -> what we wrote when it was answered here
        self.restricted = {}         # approval id -> approval, for the second "are you sure?"
        self.events_after = None
        self.ignored = set()
        self._since = None

    # -- sending -----------------------------------------------------------------------
    def send(self, chat_id, text, buttons=None, html_text=True):
        params = {"chat_id": chat_id, "text": text[:MAX_MESSAGE], "disable_web_page_preview": True}
        if html_text:
            params["parse_mode"] = "HTML"
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        return self._retrying("sendMessage", params)

    def edit(self, chat_id, message_id, text, buttons=None):
        params = {"chat_id": chat_id, "message_id": message_id, "text": text[:MAX_MESSAGE],
                  "parse_mode": "HTML", "disable_web_page_preview": True,
                  "reply_markup": {"inline_keyboard": buttons or []}}
        try:
            return self._retrying("editMessageText", params)
        except TelegramError as e:
            if "not modified" in e.description:
                return None
            raise

    def send_reply(self, chat_id, text):
        for part in chunks(text or "(Toby didn't say anything.)"):
            self.send(chat_id, html.escape(part))

    def _retrying(self, method, params, files=None):
        for _ in range(3):
            try:
                return self.tg.call(method, params, files=files, timeout=30)
            except TelegramError as e:
                if e.retry_after:
                    time.sleep(min(float(e.retry_after), 30))
                    continue
                raise
        return self.tg.call(method, params, files=files, timeout=30)

    def chats(self):
        return sorted(allowed_ids(self.config))

    # -- incoming ------------------------------------------------------------------------
    def poll_updates(self):
        """One round of getUpdates; returns how many updates were handled."""
        offset = int(self.config.get("offset", 0))
        updates = self.tg.call("getUpdates", {"offset": offset, "timeout": POLL_TIMEOUT_S,
                                              "allowed_updates": ["message", "callback_query"]},
                               timeout=POLL_TIMEOUT_S + 10)
        for update in updates or []:
            offset = max(offset, int(update["update_id"]) + 1)
            try:
                self.handle_update(update)
            except TobyUnavailable as e:
                chat = _chat_of(update)
                if chat is not None:
                    self.send(chat, html.escape(str(e)))
            except TelegramError as e:
                print(f"TELEGRAM: {e.description}", flush=True)
        if updates:
            self.config = load_config(self.config_path) or self.config
            self.config["offset"] = offset
            save_config(self.config, self.config_path)
        return len(updates or [])

    def handle_update(self, update):
        self.config = load_config(self.config_path) or self.config   # `toby telegram pair` may have added you
        if "callback_query" in update:
            return self.handle_button(update["callback_query"])
        message = update.get("message") or {}
        user = message.get("from") or {}
        chat = message.get("chat") or {}
        if chat.get("type") != "private" or int(user.get("id", 0)) not in allowed_ids(self.config):
            if user.get("id") not in self.ignored:
                self.ignored.add(user.get("id"))
                print(f"TELEGRAM: ignored a message from an unpaired account: {describe_user(user)} "
                      f"(id {user.get('id')})", flush=True)
            return
        chat_id = chat["id"]
        if message.get("voice") or message.get("audio"):
            return self.handle_voice(chat_id, message.get("voice") or message.get("audio"))
        text = str(message.get("text") or "").strip()
        if not text:
            return self.send(chat_id, "I can read text and listen to voice notes; that I can't use.")
        if text.startswith("/"):
            return self.handle_command(chat_id, text)
        self.ask(chat_id, text)

    def handle_voice(self, chat_id, voice):
        why_not = self.transcriber.unavailable() if hasattr(self.transcriber, "unavailable") else None
        if why_not:
            return self.send(chat_id, html.escape(why_not))
        info = self.tg.call("getFile", {"file_id": voice["file_id"]})
        audio = self.tg.download(info["file_path"])
        heard = self.transcriber(audio)
        if not heard:
            return self.send(chat_id, "I couldn't make out any words in that. Try again, or type it.")
        self.send(chat_id, f"Heard: <i>{html.escape(heard)}</i>")
        self.ask(chat_id, heard)

    def handle_command(self, chat_id, text):
        command, _, rest = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        rest = rest.strip()
        if command in ("/start", "/help"):
            return self.send(chat_id, html.escape(HELP))
        if command == "/pair":
            return self.send(chat_id, "This Telegram account is already paired with Toby.")
        if command == "/status":
            status, data = self.toby.get("/api/status")
            return self.send(chat_id, render_status(data) if status == 200 else html.escape(_error(data)))
        if command == "/tasks":
            status, data = self.toby.get("/api/tasks")
            tasks = (data.get("tasks") or [])[:8] if status == 200 else []
            if not tasks:
                return self.send(chat_id, "Nothing yet. Ask Toby something.")
            lines = [f"{STATE_WORDS.get(t.get('state'), t.get('state', ''))}: {html.escape(t.get('text', '')[:80])}"
                     for t in tasks]
            return self.send(chat_id, "\n".join(lines))
        if command == "/jobs":
            status, data = self.toby.get("/api/jobs")
            jobs = (data.get("jobs") or [])[:8] if status == 200 else []
            if not jobs:
                return self.send(chat_id, "No commands have run in the background.")
            lines = [f"{html.escape(j.get('name') or j.get('command', '')[:60])}: {j.get('state', '')}"
                     + (f" — {html.escape(j['last_line'][:100])}" if j.get("last_line") else "") for j in jobs]
            return self.send(chat_id, "\n".join(lines))
        if command in ("/pause", "/resume", "/stop"):
            status, data = self.toby.post(f"/api/task/{command[1:]}")
            return self.send(chat_id, html.escape(data.get("message") or _error(data)))
        if command == "/workmode":
            if rest not in ("on", "off"):
                return self.send(chat_id, "Say /workmode on or /workmode off.")
            status, data = self.toby.post("/api/work_mode", {"on": rest == "on"})
            return self.send(chat_id, html.escape(data.get("message") or _error(data)))
        if command == "/screen":
            return self.send_screen(chat_id)
        return self.send(chat_id, "I don't know that one. /help lists what I can do.")

    def send_screen(self, chat_id):
        if not self.settings().get("telegram_screenshots", False):
            return self.send(chat_id, "Screenshots aren't sent over Telegram unless you allow it, because "
                                      "Telegram's servers would see them. On the computer, set "
                                      "\"telegram_screenshots\": true in settings.json (and turn on Screen view).")
        status, data = self.toby.get("/api/screen", {"view": "full", "max": 1600}, timeout=20, raw=True)
        if status != 200:
            return self.send(chat_id, "Screen view is off on the computer, so there's nothing to show. "
                                      "Turn it on in Toby's Settings, Phone.")
        self._retrying("sendPhoto", {"chat_id": chat_id}, files={"photo": ("screen.jpg", data, "image/jpeg")})

    # -- asking Toby ---------------------------------------------------------------------
    def ask(self, chat_id, text):
        status, data = self.toby.post("/api/ask", {"text": text[:2000]})
        if status != 200 or not data.get("ok"):
            return self.send(chat_id, html.escape(data.get("message") or _error(data)))
        shown = render_task({"text": text, "state": "thinking", "steps": []})
        message = self.send(chat_id, shown)
        with self._lock:
            self.active = {"task_id": data.get("task_id"), "chat_id": chat_id, "text": text,
                           "message_id": (message or {}).get("message_id"), "shown": shown, "at": self.clock()}
        # A quick answer can finish before the state watcher knew to look for
        # it, so check the latest state once now.
        status, state = self.toby.get("/api/state")
        if status == 200:
            self._sync_task(state)

    def on_state(self, state):
        self._sync_approvals(state.get("approvals") or [])
        self._sync_task(state)

    def _sync_task(self, state):
        with self._lock:     # held throughout, so a finished task is replied to exactly once
            active = self.active
            if not active:
                return
            task = state.get("task") or {}
            if task.get("id") != active["task_id"]:
                return
            finished = task.get("state") in FINISHED and not state.get("busy")
            shown = render_task(task)
            due = self.clock() - active["at"] >= EDIT_EVERY_S
            if active["message_id"] and shown != active["shown"] and (due or finished):
                self.edit(active["chat_id"], active["message_id"], shown)
                active.update(shown=shown, at=self.clock())
            if finished:
                self.active = None
                self.send_reply(active["chat_id"], task.get("reply") or state.get("reply") or "")

    def _sync_approvals(self, pending):
        pending_ids = {a["id"] for a in pending}
        for approval in pending:
            if approval["id"] in self.approval_messages:
                continue
            text, buttons = self._approval_message(approval)
            sent = []
            for chat_id in self.chats():
                message = self.send(chat_id, text, buttons=buttons)
                if message:
                    sent.append((chat_id, message["message_id"], approval["title"]))
            self.approval_messages[approval["id"]] = sent
            if approval.get("level") == "restricted":
                self.restricted[approval["id"]] = approval
        for approval_id in [a for a in self.approval_messages if a not in pending_ids]:
            outcome = self.answered.pop(approval_id, None)
            for chat_id, message_id, title in self.approval_messages.pop(approval_id):
                note = outcome or "No longer waiting: answered on the computer or another phone, or it timed out."
                self.edit(chat_id, message_id, f"<b>{html.escape(title)}</b>\n{html.escape(note)}")
            self.restricted.pop(approval_id, None)

    @staticmethod
    def _approval_message(approval):
        lines = ["<b>Toby wants to:</b> " + html.escape(approval["title"])]
        for detail in approval.get("details") or []:
            lines.append("· " + html.escape(str(detail)))
        if approval.get("level") == "restricted":
            lines.append("<b>This is a restricted action.</b> " + html.escape(approval.get("reason") or ""))
        buttons = [[{"text": "Allow", "callback_data": f"a:{approval['id']}:y"},
                    {"text": "Don't allow", "callback_data": f"a:{approval['id']}:n"}]]
        return "\n".join(lines), buttons

    def handle_button(self, query):
        user = query.get("from") or {}
        if int(user.get("id", 0)) not in allowed_ids(self.config):
            return self.tg.call("answerCallbackQuery", {"callback_query_id": query["id"]})
        parts = str(query.get("data", "")).split(":")
        if len(parts) != 3 or parts[0] != "a":
            return self.tg.call("answerCallbackQuery", {"callback_query_id": query["id"]})
        approval_id, choice = parts[1], parts[2]
        message = query.get("message") or {}
        chat_id, message_id = (message.get("chat") or {}).get("id"), message.get("message_id")
        approval = self.restricted.get(approval_id)
        if choice == "y" and approval is not None:
            # A restricted action: say what it is once more and ask again.
            self.tg.call("answerCallbackQuery", {"callback_query_id": query["id"]})
            return self.edit(chat_id, message_id,
                             f"<b>Really allow this?</b> {html.escape(approval['title'])}\n"
                             f"{html.escape(approval.get('reason') or 'It is a restricted action.')}",
                             buttons=[[{"text": "Yes, allow it", "callback_data": f"a:{approval_id}:Y"},
                                       {"text": "No", "callback_data": f"a:{approval_id}:n"}]])
        allow = choice in ("y", "Y")
        status, data = self.toby.post("/api/approve", {"id": approval_id, "allow": allow})
        ok = status == 200 and data.get("ok")
        who = describe_user(user)
        if ok:
            self.answered[approval_id] = f"{'Allowed' if allow else 'Not allowed'} by {who}."
        self.tg.call("answerCallbackQuery", {"callback_query_id": query["id"],
                                             "text": ("Allowed" if allow else "Not allowed") if ok
                                             else "Too late: it was already answered or timed out."})

    # -- notifications ---------------------------------------------------------------------
    def poll_events(self, wait=20):
        if self.events_after is None:
            status, data = self.toby.get("/api/events", {"after": 0})
            self.events_after = data.get("last", 0) if status == 200 else 0   # only what happens from now on
            return 0
        status, data = self.toby.get("/api/events", {"after": self.events_after, "wait": wait}, timeout=wait + 10)
        if status != 200:
            return 0
        sent = 0
        for event in data.get("events") or []:
            self.events_after = max(self.events_after, event["id"])
            if event.get("kind") not in FORWARDED_EVENTS:
                continue    # tasks and questions already come through the chat itself
            text = f"<b>{html.escape(event.get('title', ''))}</b>"
            if event.get("body"):
                text += "\n" + html.escape(event["body"])
            for chat_id in self.chats():
                self.send(chat_id, text)
                sent += 1
        return sent

    # -- running ---------------------------------------------------------------------------
    def _loop(self, name, step, pause_on_error=5.0):
        while not self.stop_event.is_set():
            try:
                step()
            except TobyUnavailable as e:
                if e.code == "unpaired":
                    print("TELEGRAM: " + str(e), flush=True)
                self.stop_event.wait(pause_on_error)
            except TelegramError as e:
                if e.status == 401:
                    print("TELEGRAM: Telegram doesn't accept the bot token any more. Run: toby telegram setup",
                          flush=True)
                    self.stop_event.wait(300)
                elif e.status == 409:
                    self.stop_event.wait(10)       # `toby telegram pair` is reading the bot's messages
                else:
                    print(f"TELEGRAM {name}: {e.description}", flush=True)
                    self.stop_event.wait(e.retry_after or pause_on_error)
            except Exception as e:  # keep the service alive through anything unexpected
                print(f"TELEGRAM {name} ERROR: {e!r}", flush=True)
                self.stop_event.wait(pause_on_error)

    def _state_step(self):
        since = self._since
        status, state = self.toby.get("/api/state", {"since": since} if since is not None else None, timeout=35)
        if status != 200:
            self.stop_event.wait(5)
            return
        self._since = state.get("version")
        self.on_state(state)

    def start(self):
        threads = [threading.Thread(target=self._loop, args=("updates", self.poll_updates), daemon=True),
                   threading.Thread(target=self._loop, args=("state", self._state_step), daemon=True),
                   threading.Thread(target=self._loop, args=("events", self.poll_events), daemon=True)]
        for t in threads:
            t.start()
        return threads

    def stop(self):
        self.stop_event.set()


def _chat_of(update):
    if "callback_query" in update:
        return ((update["callback_query"].get("message") or {}).get("chat") or {}).get("id")
    return ((update.get("message") or {}).get("chat") or {}).get("id")


def _error(data):
    return (data or {}).get("error") or (data or {}).get("message") or "That didn't work."


# ---------------------------------------------------------------------------
# Pairing a Telegram account (run by `toby telegram pair`, at the computer)
# ---------------------------------------------------------------------------

def pair_account(telegram, config, config_path=None, ask=input, say=print, clock=time.time, code=None):
    """Show a one-time code, wait for it to arrive from Telegram, and ask at
    the computer whether the account that sent it is yours. Returns True if
    an account was added. The bot service must not be polling meanwhile."""
    import remote_bridge
    code = code or remote_bridge.new_code()
    username = config.get("bot_username", "")
    link = f"https://t.me/{username}?start={code}" if username else ""
    say(f"In Telegram, open @{username} and send:  /pair {remote_bridge.format_code(code)}")
    if link:
        say(f"or open this link on your phone: {link}")
        if shutil.which("qrencode"):
            subprocess.run(["qrencode", "-t", "ANSIUTF8", link])
    say("The code works once, for five minutes.")
    deadline = clock() + PAIR_TTL_S
    offset = int(config.get("offset", 0))
    wrong = 0
    while clock() < deadline:
        try:
            updates = telegram.call("getUpdates", {"offset": offset, "timeout": 20,
                                                   "allowed_updates": ["message"]}, timeout=30)
        except TelegramError as e:
            if e.status == 409:
                say("Something else is reading the bot's messages. Stop it first: systemctl --user stop toby-telegram")
                return False
            raise
        for update in updates or []:
            offset = max(offset, update["update_id"] + 1)
            message = update.get("message") or {}
            text = str(message.get("text") or "")
            user, chat = message.get("from") or {}, message.get("chat") or {}
            word, _, rest = text.partition(" ")
            if word.split("@")[0] not in ("/pair", "/start") or chat.get("type") != "private":
                continue
            if remote_bridge.normalize_code(rest) != code:
                wrong += 1
                telegram.call("sendMessage", {"chat_id": chat["id"], "text": "That code isn't right."})
                if wrong >= 5:
                    say("Too many wrong codes. Start again: toby telegram pair")
                    return False
                continue
            who = describe_user(user)
            answer = ask(f"{who} (Telegram id {user.get('id')}) sent the code. Is this you? [y/N] ")
            if str(answer).strip().lower() not in ("y", "yes"):
                telegram.call("sendMessage", {"chat_id": chat["id"], "text": "Not paired."})
                say("Not paired.")
                config["offset"] = offset
                save_config(config, config_path)
                return False
            users = [u for u in config.get("allowed_users", []) if int(u["id"]) != int(user["id"])]
            users.append({"id": int(user["id"]), "name": who, "paired_at": int(time.time())})
            config["allowed_users"] = users
            config["offset"] = offset
            save_config(config, config_path)
            telegram.call("sendMessage", {"chat_id": chat["id"],
                                          "text": "Paired. Toby is listening; say hi, or /help."})
            say(f"Paired {who}.")
            return True
    config["offset"] = offset
    save_config(config, config_path)
    say("The code expired. Start again: toby telegram pair")
    return False


def new_device_id():
    return "telegram-" + uuid.uuid4().hex[:12]


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import toby_settings
    config = load_config()
    if not config.get("bot_token") or not config.get("device_token"):
        print("Telegram isn't set up. Run: toby telegram setup", flush=True)
        return 0
    settings = toby_settings.load()
    bot = TelegramBot(TelegramAPI(config["bot_token"], config.get("api_base", API_BASE)),
                      TobyAPI(config["device_token"], int(settings.get("remote_port", 8765))),
                      settings=toby_settings.load)
    print(f"Toby is listening on Telegram as @{config.get('bot_username', '?')}, "
          f"for {len(config.get('allowed_users', []))} account(s).", flush=True)
    bot.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        bot.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
