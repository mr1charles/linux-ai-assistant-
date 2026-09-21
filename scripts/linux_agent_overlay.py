#!/usr/bin/env python3
"""
Linux Agent — Overlay mode.
A toggleable floating text box (Rofi/Spotlight-style) on Hyprland, instead of
a wake word. Type anything: desktop commands, questions, coding help, or
"check my email from ...". Local LLM (Ollama) still does the thinking.

Network calls, and ONLY these: Discord webhook (if you ask it to message
someone) and Gmail IMAP (if you ask it to check email). Everything else —
parsing, desktop control, code explanations — stays local.

Setup:
  Ollama:
    pacman -S ollama && ollama serve &
    ollama pull qwen2.5:1.5b

  GTK overlay deps (Arch/CachyOS):
    sudo pacman -S gtk3 python-gobject gtk-layer-shell

  Python deps:
    pip install requests PyGObject

  Gmail (optional, only used if you ask it to check email):
    1. Enable IMAP: Gmail Settings -> Forwarding and POP/IMAP -> Enable IMAP
    2. Create an App Password: https://myaccount.google.com/apppasswords
       (requires 2-Step Verification to be on)
    export EMAIL_ADDRESS="you@gmail.com"
    export EMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"
    export IMAP_SERVER="imap.gmail.com"      # change if not Gmail

  Discord (optional):
    export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."

Run:
  python3 linux_agent_overlay.py &

Toggle show/hide (bind this to a Hyprland keybind, e.g. SUPER+SPACE):
  pkill -SIGUSR1 -f linux_agent_overlay.py

Hyprland config line:
  bind = SUPER, space, exec, pkill -SIGUSR1 -f linux_agent_overlay.py
"""

import email
import imaplib
import json
import os
import signal
import subprocess
from email.header import decode_header

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
from gi.repository import Gdk, GLib, Gtk, GtkLayerShell  # noqa: E402

import requests  # noqa: E402

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/chat")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
EMAIL_ADDRESS = os.environ.get("EMAIL_ADDRESS")
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD")
IMAP_SERVER = os.environ.get("IMAP_SERVER", "imap.gmail.com")
MAX_HISTORY_TURNS = 6

SYSTEM_PROMPT = """You are a helpful assistant running locally on a Linux desktop \
(Hyprland window manager), accessed through a typed overlay box.

Respond with ONLY a JSON object, no prose, no markdown fences, shaped exactly like:
{"actions": [...], "reply": "..."}

"actions" is an ordered list of desktop commands (empty list if just chatting/coding/asking \
a question). Each action is one of:
  {"tool": "open_url", "url": "https://..."}
  {"tool": "open_app", "command": "app-executable-name"}
  {"tool": "send_discord_message", "content": "message text"}
  {"tool": "close_active_window"}
  {"tool": "read_emails", "from_contains": "sender name or address, or empty for any", \
"subject_contains": "keyword or empty", "count": 5}

"reply" is shown in the overlay text box — always fill this in.
- For commands: a short natural confirmation.
- For coding questions: write the actual code/explanation in "reply" (you are NOT allowed \
to write files, only show code as text).
- For email requests ("check my email from bob", "any emails about the invoice"): use the \
read_emails action with the relevant filters; leave "reply" as a short "Checking..." — the \
program will append the real results after fetching.
- For general chat/questions: just answer normally and conversationally, using conversation \
history for context.

Rules:
- A site name like "youtube" or "tiktok" -> open_url with its https:// homepage.
- A native app name (firefox, kitty, code, spotify, etc.) -> open_app.
- Preserve the order the user asked for multi-step requests.
"""

# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def open_url(url: str):
    if not url.startswith("http"):
        url = "https://" + url
    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"Opened {url}"


def open_app(command: str):
    subprocess.Popen(command.split(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"Launched {command}"


def send_discord_message(content: str):
    if not DISCORD_WEBHOOK_URL:
        return "No DISCORD_WEBHOOK_URL configured — skipped."
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=10)
    if resp.status_code in (200, 204):
        return f"Sent Discord message: {content!r}"
    return f"Discord send failed ({resp.status_code}): {resp.text}"


def close_active_window():
    subprocess.run(["hyprctl", "dispatch", "killactive"])
    return "Closed active window"


def _decode(value):
    if value is None:
        return ""
    parts = decode_header(value)
    out = []
    for text, enc in parts:
        out.append(text.decode(enc or "utf-8", errors="replace") if isinstance(text, bytes) else text)
    return "".join(out)


def read_emails(from_contains: str = "", subject_contains: str = "", count: int = 5):
    if not (EMAIL_ADDRESS and EMAIL_APP_PASSWORD):
        return "Email isn't configured — set EMAIL_ADDRESS and EMAIL_APP_PASSWORD."
    try:
        m = imaplib.IMAP4_SSL(IMAP_SERVER)
        m.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
        m.select("INBOX")

        criteria = []
        if from_contains:
            criteria += ["FROM", f'"{from_contains}"']
        if subject_contains:
            criteria += ["SUBJECT", f'"{subject_contains}"']
        search_expr = " ".join(criteria) if criteria else "ALL"

        typ, data = m.search(None, search_expr)
        ids = data[0].split()[-count:] if data[0] else []
        if not ids:
            m.logout()
            return "No matching emails found."

        results = []
        for msg_id in reversed(ids):
            typ, msg_data = m.fetch(msg_id, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            results.append(f"From: {_decode(msg.get('From'))} | Subject: {_decode(msg.get('Subject'))}")
        m.logout()
        return "\n".join(results)
    except Exception as e:
        return f"Email check failed: {e}"


DISPATCH = {
    "open_url": lambda a: open_url(a["url"]),
    "open_app": lambda a: open_app(a["command"]),
    "send_discord_message": lambda a: send_discord_message(a["content"]),
    "close_active_window": lambda a: close_active_window(),
    "read_emails": lambda a: read_emails(
        a.get("from_contains", ""), a.get("subject_contains", ""), a.get("count", 5)
    ),
}

# ---------------------------------------------------------------------------
# Local LLM call
# ---------------------------------------------------------------------------

def think(instruction: str, history: list) -> dict:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history[-MAX_HISTORY_TURNS * 2:])
    messages.append({"role": "user", "content": instruction})

    payload = {"model": OLLAMA_MODEL, "messages": messages, "stream": False, "format": "json"}
    try:
        resp = requests.post(OLLAMA_URL, json=payload, timeout=60)
        resp.raise_for_status()
    except requests.exceptions.ConnectionError:
        return {"actions": [], "reply": "Can't reach Ollama — is `ollama serve` running?"}

    raw = resp.json()["message"]["content"].strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"actions": [], "reply": raw[:800]}

    parsed.setdefault("actions", [])
    parsed.setdefault("reply", "")
    return parsed


def run_instruction(instruction: str, history: list) -> str:
    result = think(instruction, history)
    output_lines = []
    for action in result["actions"]:
        tool = action.get("tool")
        fn = DISPATCH.get(tool)
        if not fn:
            output_lines.append(f"[unknown tool: {tool}]")
            continue
        outcome = fn(action)
        output_lines.append(outcome)

    reply = result["reply"]
    if output_lines:
        reply = (reply + "\n" if reply else "") + "\n".join(output_lines)

    history.append({"role": "user", "content": instruction})
    history.append({"role": "assistant", "content": result["reply"]})
    return reply

# ---------------------------------------------------------------------------
# GTK overlay
# ---------------------------------------------------------------------------

class OverlayWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Agent")
        self.history = []

        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.TOP, True)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.TOP, 120)
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.ON_DEMAND)

        self.set_default_size(640, 80)
        self.set_decorated(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_border_width(10)
        self.add(box)

        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("Ask me anything, or tell me to do something...")
        self.entry.connect("activate", self.on_submit)
        self.entry.connect("key-press-event", self.on_key)
        box.pack_start(self.entry, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.set_min_content_height(260)
        self.output = Gtk.TextView()
        self.output.set_editable(False)
        self.output.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.output.modify_font_easy = None
        scroller.add(self.output)
        box.pack_start(scroller, True, True, 0)

        self.connect("destroy", Gtk.main_quit)
        self.show_all()
        self.set_visible(False)

    def on_key(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.set_visible(False)
        return False

    def on_submit(self, widget):
        text = self.entry.get_text().strip()
        if not text:
            return
        self.entry.set_text("")
        self.append_output(f"> {text}")
        GLib.idle_add(self.process, text)

    def append_output(self, text):
        buf = self.output.get_buffer()
        end = buf.get_end_iter()
        buf.insert(end, text + "\n\n")
        GLib.idle_add(lambda: self.output.scroll_to_iter(buf.get_end_iter(), 0, False, 0, 0))

    def process(self, text):
        reply = run_instruction(text, self.history)
        self.append_output(reply)
        return False

    def toggle(self, *_args):
        if self.get_visible():
            self.set_visible(False)
        else:
            self.set_visible(True)
            self.present()
            self.entry.grab_focus()


def main():
    win = OverlayWindow()

    def handle_usr1(signum, frame):
        GLib.idle_add(win.toggle)

    signal.signal(signal.SIGUSR1, handle_usr1)
    # Let GLib's main loop process unix signals promptly
    GLib.timeout_add(200, lambda: True)

    Gtk.main()


if __name__ == "__main__":
    main()
