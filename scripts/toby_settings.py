"""
toby_settings.py — persisted user preferences for Little Toby's UI/behavior.

Deliberately small and flat: a single JSON file, loaded once at startup and
re-read/written whenever the Settings page saves. Anything not present in a
saved file falls back to DEFAULTS, so adding a new setting later never
breaks an existing settings.json.
"""

import json
from pathlib import Path

SETTINGS_PATH = Path.home() / "linux-agent" / "settings.json"

DEFAULTS = {
    "response_style": "balanced",     # "concise" | "balanced" | "detailed"
    "memory_enabled": True,            # knowledge graph + study notes extraction
    "notifications_enabled": True,     # Dynamic Island "reply ready" popups
    "accent_color": "#5a8cff",         # sidebar/nav accent, hex
    "ollama_model": "",                # "" = use the script's built-in default
    "voice_mode_enabled": False,       # Voice Mode off by default — mic access is opt-in
    "voice_wake_word_enabled": True,   # if off, every utterance is treated as a command (no wake word needed)
    "voice_name": "en-us",             # espeak-ng voice preset
    "voice_rate": 165,                 # espeak-ng -s (words per minute)
    "voice_pitch": 50,                 # espeak-ng -p (0-99)
    "camera_mode_enabled": False,      # Camera Mode off by default — webcam access is opt-in
    "camera_pinch_cursor": False,      # pointing with your hand moves the real cursor — needs the same
                                        # one-time mouse-control confirmation as any other pointer action
    "memory_graph_layout": "force",    # "force" | "radial" | "mindmap"
    "cloud_provider": "openai",        # only OpenAI is wired up for now
    "cloud_api_key": "",               # stored locally only — never committed, never sent anywhere but the provider
    "cloud_model": "gpt-4o",
    "voice_auto_cloud": False,          # opt-in: voice-triggered requests use cloud automatically for speed+quality
    "grade_level": "9th-10th",         # adjusts explanation complexity for study features

    # -- speed ---------------------------------------------------------------
    "fast_path_enabled": True,         # obvious one-step commands ("open youtube", "close this tab")
                                        # run instantly without waiting on the model

    # -- the chibi and the look ----------------------------------------------
    "summon_keybind": "SUPER, G",      # bound at runtime if the key is free; "" to skip
    "startup_greeting": True,          # Toby pops up briefly when you log in, then tucks away
    "animations": {},                  # overrides for toby_anim.ANIMATION_DEFAULTS — see README

    # -- fingerprint ---------------------------------------------------------
    "confirm_with_fingerprint": False, # approve mouse/keyboard/install prompts with fprintd
                                        # instead of (as well as) the Yes button

    # -- phone app -----------------------------------------------------------
    "remote_enabled": False,           # the phone app bridge — off until you pair a phone
    "remote_port": 8765,
    "remote_bind": "127.0.0.1",        # loopback only; Tailscale serve publishes it privately
}

GRADE_LEVELS = ["5th-6th", "7th-8th", "9th-10th", "11th-12th", "College"]

MEMORY_GRAPH_LAYOUTS = [
    ("force", "Force-directed"),
    ("radial", "Radial"),
    ("mindmap", "Mind map"),
]

RESPONSE_STYLE_PROMPTS = {
    "concise": "Keep replies short — a sentence or two — unless the user explicitly asks for more detail.",
    "balanced": "Keep replies natural length: enough to be genuinely useful, without padding.",
    "detailed": "Feel free to give thorough, well-explained replies with reasoning, not just the bottom line.",
}

RESPONSE_STYLES = ["concise", "balanced", "detailed"]


def load() -> dict:
    settings = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    if SETTINGS_PATH.exists():
        try:
            saved = json.loads(SETTINGS_PATH.read_text())
            if isinstance(saved, dict):
                for key, value in saved.items():
                    if key not in DEFAULTS:
                        continue
                    if isinstance(DEFAULTS[key], dict) and isinstance(value, dict):
                        # nested groups (animations) merge rather than replace,
                        # so one saved override doesn't drop every other default
                        settings[key] = {**DEFAULTS[key], **value}
                    else:
                        settings[key] = value
        except (json.JSONDecodeError, OSError):
            pass
    return settings


def save(settings: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: settings.get(k, DEFAULTS[k]) for k in DEFAULTS}
    SETTINGS_PATH.write_text(json.dumps(clean, indent=2))


def style_prompt_line(settings: dict) -> str:
    style = settings.get("response_style", "balanced")
    return RESPONSE_STYLE_PROMPTS.get(style, RESPONSE_STYLE_PROMPTS["balanced"])
