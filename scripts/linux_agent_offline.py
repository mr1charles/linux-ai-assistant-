#!/usr/bin/env python3
"""
Linux Agent — OFFLINE FIRST.
- Command understanding: local LLM via Ollama (no internet)
- Voice input: local Vosk model (no internet)
- Everything executes locally (xdg-open, hyprctl, subprocess)
- The ONLY network call is the Discord webhook, because sending a
  Discord message is inherently an online action.

Setup:
  1. Install Ollama:            https://ollama.com  (or `pacman -S ollama` on CachyOS)
     Start it:                  systemctl --user start ollama   (or `ollama serve`)
     Pull a small model:        ollama pull qwen2.5:1.5b
     (qwen2.5:1.5b / llama3.2:1b are both light enough for a Chromebook-class CPU)

  2. Install python deps:
     pip install requests vosk sounddevice

  3. Download a small offline Vosk speech model (~50MB), unzip it, and point
     VOSK_MODEL_PATH at the folder:
     https://alphacephei.com/vosk/models  -> "vosk-model-small-en-us-0.15"

  4. Only if you want Discord sending to work:
     export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."

Usage:
  python3 linux_agent.py --text "open youtube then tiktok"
  python3 linux_agent.py --voice
  python3 linux_agent.py             # interactive: type, or 'v' + Enter to speak
"""

import argparse
import json
import os
import subprocess
import sys

import requests

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/chat")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
VOSK_MODEL_PATH = os.environ.get("VOSK_MODEL_PATH", "./vosk-model-small-en-us-0.15")

# ---------------------------------------------------------------------------
# Prompt: ask the local model to return ONLY a JSON list of actions.
# Small local models are less reliable than hosted ones at strict formatting,
# so the prompt is deliberately blunt and we validate/clean the output.
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are a command parser for a Linux desktop agent (Hyprland window manager).
Convert the user's instruction into a JSON array of actions, in execution order.
Respond with ONLY the JSON array. No prose, no markdown fences.

Each action is one of:
  {"tool": "open_url", "url": "https://..."}
  {"tool": "open_app", "command": "app-executable-name"}
  {"tool": "send_discord_message", "content": "message text"}
  {"tool": "close_active_window"}

Rules:
- A site name like "youtube" or "tiktok" -> open_url with its https:// homepage.
- A native app name (firefox, kitty, code, spotify, etc.) -> open_app.
- "message/tell/say on discord ..." -> send_discord_message.
- Preserve the order the user asked for.
- If nothing matches, return [].

Example:
User: "open youtube then tiktok and tell discord I'm live"
[{"tool":"open_url","url":"https://youtube.com"},{"tool":"open_url","url":"https://tiktok.com"},{"tool":"send_discord_message","content":"I'm live"}]
"""

# ---------------------------------------------------------------------------
# Tool implementations (local side effects)
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
        return "No DISCORD_WEBHOOK_URL configured — skipped (this is the only online step)."
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=10)
    if resp.status_code in (200, 204):
        return f"Sent Discord message: {content!r}"
    return f"Discord send failed ({resp.status_code}): {resp.text}"


def close_active_window():
    subprocess.run(["hyprctl", "dispatch", "killactive"])
    return "Closed active window"


DISPATCH = {
    "open_url": lambda a: open_url(a["url"]),
    "open_app": lambda a: open_app(a["command"]),
    "send_discord_message": lambda a: send_discord_message(a["content"]),
    "close_active_window": lambda a: close_active_window(),
}

# ---------------------------------------------------------------------------
# Local LLM call (Ollama, runs on localhost — no external network)
# ---------------------------------------------------------------------------

def parse_instruction(instruction: str):
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": instruction},
        ],
        "stream": False,
        "format": "json",
    }
    try:
        resp = requests.post(OLLAMA_URL, json=payload, timeout=60)
        resp.raise_for_status()
    except requests.exceptions.ConnectionError:
        print("Could not reach Ollama at", OLLAMA_URL, "- is `ollama serve` running?")
        return []

    raw = resp.json()["message"]["content"].strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        actions = json.loads(raw)
        if isinstance(actions, dict):
            actions = actions.get("actions", [actions])
        return actions
    except json.JSONDecodeError:
        print("Model returned non-JSON, ignoring:", raw[:200])
        return []


def run_instruction(instruction: str):
    actions = parse_instruction(instruction)
    if not actions:
        print("No actions understood.")
        return
    for action in actions:
        tool = action.get("tool")
        fn = DISPATCH.get(tool)
        if not fn:
            print(f"  -> unknown tool: {tool}")
            continue
        result = fn(action)
        print(f"  -> {tool}({action}) => {result}")

# ---------------------------------------------------------------------------
# Offline voice input via Vosk
# ---------------------------------------------------------------------------

def listen_voice() -> str:
    import queue

    import sounddevice as sd
    from vosk import KaldiRecognizer, Model

    if not os.path.isdir(VOSK_MODEL_PATH):
        print(f"Vosk model not found at {VOSK_MODEL_PATH}. See setup instructions at top of file.")
        return ""

    model = Model(VOSK_MODEL_PATH)
    rec = KaldiRecognizer(model, 16000)
    q = queue.Queue()

    def callback(indata, frames, time_info, status):
        q.put(bytes(indata))

    print("Listening (offline)... speak now.")
    with sd.RawInputStream(samplerate=16000, blocksize=8000, dtype="int16",
                            channels=1, callback=callback):
        silence_chunks = 0
        while True:
            data = q.get()
            if rec.AcceptWaveform(data):
                result = json.loads(rec.Result())
                text = result.get("text", "")
                if text:
                    return text
                silence_chunks += 1
                if silence_chunks > 3:
                    return ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", help="Run a single text command and exit")
    parser.add_argument("--voice", action="store_true", help="Run a single voice command and exit")
    args = parser.parse_args()

    if args.text:
        run_instruction(args.text)
        return
    if args.voice:
        text = listen_voice()
        if text:
            print("Heard:", text)
            run_instruction(text)
        return

    print("Interactive mode (offline). Type a command, or 'v' + Enter to speak. Ctrl+C to quit.")
    while True:
        try:
            raw = input("\n> ").strip()
        except (KeyboardInterrupt, EOFError):
            break
        if not raw:
            continue
        if raw.lower() == "v":
            heard = listen_voice()
            if not heard:
                continue
            print("Heard:", heard)
            run_instruction(heard)
        else:
            run_instruction(raw)


if __name__ == "__main__":
    main()
