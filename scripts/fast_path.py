"""
fast_path.py — answer the obvious requests instantly, without the model.

"open youtube", "close this tab", "study mode on", "what time is it". On a
CPU-only laptop the local model takes seconds to handle any of these, yet
there is exactly one sensible thing to do for each. So they are recognised
here, by plain matching, and done immediately.

The rule that keeps this safe: a request is only handled here if the WHOLE
utterance matches a known shape. "open youtube" is handled; "open youtube
and find a video about photosynthesis" is not, because that needs the
model's judgement about what to search for. Anything that isn't a complete,
unambiguous match returns None and goes to the model exactly as before. A
fast path that guesses would be worse than none.

Returns the same shape the model does: {"actions": [...], "reply": ..., "mood": ...}.
"""

import datetime
import re

# Sites that are always a web address, never an installed app.
SITES = {
    "youtube": "https://www.youtube.com",
    "tiktok": "https://www.tiktok.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "google classroom": "https://classroom.google.com",
    "classroom": "https://classroom.google.com",
    "google docs": "https://docs.google.com",
    "google drive": "https://drive.google.com",
    "google calendar": "https://calendar.google.com",
    "khan academy": "https://www.khanacademy.org",
    "kami": "https://web.kamihq.com",
    "quizlet": "https://quizlet.com",
    "github": "https://github.com",
    "reddit": "https://www.reddit.com",
    "netflix": "https://www.netflix.com",
    "twitch": "https://www.twitch.tv",
    "wikipedia": "https://en.wikipedia.org",
    "chatgpt": "https://chatgpt.com",
    "claude": "https://claude.ai",
    "outlook": "https://outlook.live.com",
    "canvas": "https://canvas.instructure.com",
    "schoology": "https://app.schoology.com",
    "desmos": "https://www.desmos.com/calculator",
}

# Words people wrap around a command that don't change what it means.
_LEADING = re.compile(
    r"^(?:(?:hey|hi|ok|okay|yo)\s+)?(?:toby[,\s]+)?"
    r"(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?(?:go\s+ahead\s+and\s+)?"
    r"(?:(?:i\s+want\s+to|i'd\s+like\s+to|let's|lets)\s+)?"
)
_TRAILING = re.compile(r"(?:\s+(?:please|for me|now|real quick|thanks|thank you))+$")
_JOINERS = re.compile(r"\s*(?:,\s*and|,|\band then\b|\bthen\b|\band\b)\s*")


def normalize(text):
    t = str(text or "").strip().lower()
    t = re.sub(r"[.!?]+$", "", t).strip()
    t = _LEADING.sub("", t).strip()
    t = _TRAILING.sub("", t).strip()
    return re.sub(r"\s+", " ", t)


def _open_target(name, allowed_apps):
    name = name.strip()
    name = re.sub(r"^(?:up\s+)?(?:the\s+)?", "", name)
    name = re.sub(r"\s+(?:app|website|site|page)$", "", name)
    if name in SITES:
        return {"tool": "open_url", "url": SITES[name]}, name
    if name in allowed_apps:
        return {"tool": "open_app", "command": name}, name
    if re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/\S*)?", name):
        return {"tool": "open_url", "url": "https://" + name}, name
    return None, name


def _nice(names):
    names = [n.title() if n not in ("github", "tiktok", "youtube") else
             {"github": "GitHub", "tiktok": "TikTok", "youtube": "YouTube"}[n] for n in names]
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def match(text, allowed_apps=(), now=None):
    """The fast result for text, or None to let the model handle it."""
    t = normalize(text)
    if not t or len(t) > 80:
        return None
    now = now or datetime.datetime.now()

    # -- Study Mode ------------------------------------------------------------------
    if re.fullmatch(r"(?:turn on |start |enable )?study mode(?: on)?|start studying|(?:turn |switch )?on study mode", t):
        return {"actions": [{"tool": "enable_study_mode"}], "reply": "Study Mode is on.", "mood": "happy"}
    if re.fullmatch(r"(?:turn off |stop |end |disable )(?:study mode|studying)|study mode off|(?:turn |switch )?off study mode", t):
        return {"actions": [{"tool": "disable_study_mode"}], "reply": "Study Mode is off.", "mood": "neutral"}

    # -- open one or more known things -----------------------------------------
    m = re.fullmatch(r"(?:open|launch|start|pull up|bring up|go to)\s+(.+)", t)
    if m:
        parts = [p for p in _JOINERS.split(m.group(1)) if p and p.strip()]
        actions, names = [], []
        for part in parts:
            action, name = _open_target(part, allowed_apps)
            if action is None:
                return None   # one unknown target: the model decides, not us
            actions.append(action)
            names.append(name)
        if actions and len(actions) <= 4:
            return {"actions": actions, "reply": f"Opening {_nice(names)}.", "mood": "happy"}
        return None

    # -- closing things ------------------------------------------------------------
    if re.fullmatch(r"close (?:this |the |that |my |current )?(?:browser )?tab", t):
        return {"actions": [{"tool": "close_tab"}], "reply": "Closed the tab.", "mood": "neutral"}
    if re.fullmatch(r"close (?:this |the |that |current )?(?:window|app)", t):
        return {"actions": [{"tool": "close_active_window"}], "reply": "Closed it.", "mood": "neutral"}

    # -- handing back control ---------------------------------------------------------
    if re.fullmatch(r"stop (?:controlling|using) (?:my |the )?(?:mouse|keyboard|screen)|give me (?:back )?control", t):
        return {"actions": [{"tool": "disable_control"}],
                "reply": "Done — I won't touch the mouse or keyboard again this session.", "mood": "neutral"}

    # -- time and date ----------------------------------------------------------------
    if re.fullmatch(r"what(?:'s| is) the time|what time is it|time", t):
        return {"actions": [], "reply": f"It's {now.strftime('%-I:%M %p')}.", "mood": "neutral"}
    if re.fullmatch(r"what(?:'s| is) (?:the date|today(?:'s date)?)|what day is (?:it|today)|today's date", t):
        return {"actions": [], "reply": f"It's {now.strftime('%A, %B %-d')}.", "mood": "neutral"}

    return None
