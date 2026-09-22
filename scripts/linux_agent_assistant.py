#!/usr/bin/env python3
"""
Linux Agent — "assistant mode" (Bixby/Mi-Claw style).
Fully offline: wake word, local LLM (Ollama), local speech-to-text (Vosk),
local text-to-speech (espeak-ng), conversation memory.
The ONLY network call in the whole thing is the Discord webhook, and only
when you actually ask it to send a Discord message.

Setup:
  Ollama:
    pacman -S ollama            # or ollama.com installer
    ollama serve &
    ollama pull qwen2.5:1.5b

  Python deps:
    pip install requests vosk sounddevice

  Offline STT model (~50MB, one-time):
    https://alphacephei.com/vosk/models -> vosk-model-small-en-us-0.15
    unzip it next to this script (or set VOSK_MODEL_PATH)

  Offline TTS (pick one, espeak-ng is default/lightest):
    sudo pacman -S espeak-ng
    # nicer voice, still offline, heavier: sudo pacman -S piper-tts (optional, see speak())

  Optional (only touches network if used):
    export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."

Usage:
  python3 linux_agent_assistant.py --assistant       # always-on, wake word "computer"
  python3 linux_agent_assistant.py --text "..."       # one-shot text command
  python3 linux_agent_assistant.py --voice            # one-shot voice command
  python3 linux_agent_assistant.py                    # interactive typing loop
"""

import argparse
import json
import os
import queue
import subprocess

import requests

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/chat")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
VOSK_MODEL_PATH = os.environ.get("VOSK_MODEL_PATH", "./vosk-model-small-en-us-0.15")
WAKE_WORD = os.environ.get("WAKE_WORD", "computer")
MAX_HISTORY_TURNS = 6  # how many past exchanges it remembers

SYSTEM_PROMPT = """You are a helpful, conversational voice assistant running locally on a Linux \
desktop (Hyprland window manager). You can both chat normally AND control the desktop.

Respond with ONLY a JSON object, no prose, no markdown fences, shaped exactly like:
{"actions": [...], "reply": "..."}

"actions" is an ordered list of desktop commands to run (empty list if the user is just \
chatting or asking a question). Each action is one of:
  {"tool": "open_url", "url": "https://..."}
  {"tool": "open_app", "command": "app-executable-name"}
  {"tool": "send_discord_message", "content": "message text"}
  {"tool": "close_active_window"}

"reply" is what you say back out loud — always fill this in. For commands, a short natural \
confirmation ("Opening YouTube and TikTok."). For general questions or chat, answer normally \
and conversationally, like a helpful assistant, using the conversation history for context.

Rules:
- A site name like "youtube" or "tiktok" -> open_url with its https:// homepage.
- A native app name (firefox, kitty, code, spotify, etc.) -> open_app.
- "message/tell/say on discord ..." -> send_discord_message.
- Preserve the order the user asked for.
- If it's just conversation (greetings, questions, opinions, small talk), leave actions empty \
and just reply.
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
        return "No DISCORD_WEBHOOK_URL configured — skipped."
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
# Offline text-to-speech
# ---------------------------------------------------------------------------

def speak(text: str):
    if not text:
        return
    print(f"[assistant] {text}")
    try:
        subprocess.run(["espeak-ng", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        # espeak-ng not installed — fail silently, text is already printed above
        pass

# ---------------------------------------------------------------------------
# Local LLM call with conversation memory
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
        return {"actions": [], "reply": "I can't reach Ollama — is `ollama serve` running?"}

    raw = resp.json()["message"]["content"].strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"actions": [], "reply": raw[:300]}  # model ignored JSON instruction, just say it

    parsed.setdefault("actions", [])
    parsed.setdefault("reply", "")
    return parsed


def run_instruction(instruction: str, history: list):
    result = think(instruction, history)
    for action in result["actions"]:
        tool = action.get("tool")
        fn = DISPATCH.get(tool)
        if not fn:
            print(f"  -> unknown tool: {tool}")
            continue
        outcome = fn(action)
        print(f"  -> {tool}({action}) => {outcome}")

    speak(result["reply"])

    history.append({"role": "user", "content": instruction})
    history.append({"role": "assistant", "content": result["reply"]})
    return result

# ---------------------------------------------------------------------------
# Offline speech recognition (one-shot capture, used both for wake word and commands)
# ---------------------------------------------------------------------------

def _listen_raw(timeout_silence_chunks=4) -> str:
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

    with sd.RawInputStream(samplerate=16000, blocksize=8000, dtype="int16",
                            channels=1, callback=callback):
        silence = 0
        while True:
            data = q.get()
            if rec.AcceptWaveform(data):
                text = json.loads(rec.Result()).get("text", "")
                if text:
                    return text
                silence += 1
                if silence > timeout_silence_chunks:
                    return ""


def listen_voice() -> str:
    print("Listening (offline)... speak now.")
    return _listen_raw()


def assistant_loop():
    print(f'Assistant mode. Say "{WAKE_WORD}" to activate. Ctrl+C to quit.')
    speak(f"I'm listening for {WAKE_WORD}.")
    history = []
    while True:
        try:
            heard = _listen_raw()
        except KeyboardInterrupt:
            break
        if not heard:
            continue
        if WAKE_WORD.lower() not in heard.lower():
            continue

        # strip the wake word off if the user said it plus a command in one breath
        remainder = heard.lower().replace(WAKE_WORD.lower(), "", 1).strip()
        if not remainder:
            speak("Yes?")
            remainder = listen_voice()
            if not remainder:
                continue

        print("Heard:", remainder)
        run_instruction(remainder, history)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", help="Run a single text command and exit")
    parser.add_argument("--voice", action="store_true", help="Run a single voice command and exit")
    parser.add_argument("--assistant", action="store_true", help="Always-on wake-word assistant mode")
    args = parser.parse_args()

    if args.assistant:
        assistant_loop()
        return
    if args.text:
        run_instruction(args.text, [])
        return
    if args.voice:
        text = listen_voice()
        if text:
            print("Heard:", text)
            run_instruction(text, [])
        return

    print("Interactive mode. Type a command/question, or 'v' + Enter to speak. Ctrl+C to quit.")
    history = []
    while True:
        try:
            raw = input("\n> ").strip()
        except (KeyboardInterrupt, EOFError):
            break
        if not raw:
            continue
        instruction = listen_voice() if raw.lower() == "v" else raw
        if not instruction:
            continue
        run_instruction(instruction, history)


if __name__ == "__main__":
    main()
