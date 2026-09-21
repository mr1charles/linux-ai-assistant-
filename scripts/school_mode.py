"""
school_mode.py — scheduled and adaptive activation of Study Mode.

Schedule is per-day (a day with no entry just doesn't activate), settable
via the set_school_schedule tool from natural conversation rather than
hand-editing JSON.
"""

import json
from datetime import datetime, time as dtime
from pathlib import Path

CONFIG_PATH = Path.home() / "linux-agent" / "school_mode.json"

DEFAULT_CONFIG = {
    "scheduled_enabled": False,
    "schedule": {},  # e.g. {"mon": {"start": "07:30", "end": "19:00"}, ...}
    "adaptive_enabled": False,
    "adaptive_keywords": [
        "classroom.google.com",
        "kami",
    ],
}

_DAY_ALIASES = {
    "monday": "mon", "mon": "mon",
    "tuesday": "tue", "tue": "tue", "tues": "tue",
    "wednesday": "wed", "wed": "wed",
    "thursday": "thu", "thu": "thu", "thurs": "thu",
    "friday": "fri", "fri": "fri",
    "saturday": "sat", "sat": "sat",
    "sunday": "sun", "sun": "sun",
}


def load_config():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
        return dict(DEFAULT_CONFIG)
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        return cfg
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_CONFIG)


def save_config(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def set_day_schedule(day: str, start: str, end: str) -> str:
    """day: full or short name (case-insensitive). start/end: 'HH:MM' 24hr."""
    key = _DAY_ALIASES.get(day.strip().lower())
    if not key:
        return f"'{day}' isn't a recognized day name."
    try:
        dtime.fromisoformat(start)
        dtime.fromisoformat(end)
    except ValueError:
        return f"Times must be 24-hour HH:MM (got start={start!r}, end={end!r})."

    cfg = load_config()
    cfg["schedule"][key] = {"start": start, "end": end}
    cfg["scheduled_enabled"] = True
    save_config(cfg)
    return f"Set {key} schedule: {start}\u2013{end}. Scheduled Study Mode is now enabled."


def is_in_schedule(config) -> bool:
    if not config.get("scheduled_enabled"):
        return False
    now = datetime.now()
    day = now.strftime("%a").lower()[:3]
    day_window = config.get("schedule", {}).get(day)
    if not day_window:
        return False
    try:
        start = dtime.fromisoformat(day_window["start"])
        end = dtime.fromisoformat(day_window["end"])
    except (ValueError, KeyError):
        return False
    return start <= now.time() <= end


def screen_matches_keywords(screen_text: str, keywords) -> bool:
    if not screen_text or not keywords:
        return False
    lower = screen_text.lower()
    return any(kw.lower() in lower for kw in keywords)
