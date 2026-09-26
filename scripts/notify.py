"""
notify.py — which of Toby's events become notifications, and where they go.

The phone apps fetch events from the computer themselves (see EventLog in
remote_bridge.py). That's instant while the app is open, but iOS only lets
an app check in now and then once it's in the background, so a
notification can arrive late or not at all.

For notifications that always arrive, Toby can also send them through ntfy
(https://ntfy.sh), a free push service with iPhone and Android apps: set
"notify_ntfy_url" in settings.json to a topic URL only you know, e.g.
https://ntfy.sh/toby-4f9c2a7e1b, and subscribe to it in the ntfy app. It's
off by default because the text then passes through ntfy's server (or your
own, if you run one); Toby sends only the short title and one line, never
file contents or command output.

Proper Apple push notifications to the Little Toby app itself need a paid
Apple Developer account and a push key on the computer; see
docs/COMPANION.md for what that would take.
"""

import threading
import urllib.parse
import urllib.request

# kind -> the settings key that turns it off, and whether it's on by default
CATEGORIES = {
    "task_done": True,          # a task you started from the phone finished
    "task_failed": True,
    "needs_permission": True,   # Toby is waiting for your yes
    "job_done": True,           # a build, test run or download finished
    "job_failed": True,
    "power": True,              # the computer is going to sleep or shutting down
    "device": True,             # a phone was paired or unpaired
    "queue": True,              # the morning tally of what the overnight queue did
}


def wanted(kind, settings):
    chosen = (settings or {}).get("notify_categories", {})
    return bool(chosen.get(kind, CATEGORIES.get(kind, True)))


class NtfySender:
    """Posts events to an ntfy topic, off the caller's thread."""

    def __init__(self, url, settings_getter=dict, opener=urllib.request.urlopen):
        self.url = str(url or "").strip()
        self.settings = settings_getter
        self.opener = opener

    @staticmethod
    def valid(url):
        parsed = urllib.parse.urlparse(str(url or ""))
        local = parsed.hostname in ("localhost", "127.0.0.1") or str(parsed.hostname or "").endswith(".ts.net")
        return parsed.scheme == "https" or (parsed.scheme == "http" and local)

    def __call__(self, event):
        if not self.valid(self.url) or not wanted(event.get("kind"), self.settings()):
            return False
        threading.Thread(target=self._send, args=(event,), daemon=True).start()
        return True

    def _send(self, event):
        body = (event.get("body") or event.get("title") or "").encode()[:300]
        req = urllib.request.Request(self.url, data=body, method="POST")
        # HTTP headers are latin-1; keep the title plain
        req.add_header("Title", event.get("title", "Little Toby").encode("ascii", "replace").decode())
        req.add_header("Tags", "robot")
        if event.get("kind") in ("needs_permission", "task_failed", "job_failed"):
            req.add_header("Priority", "high")
        try:
            with self.opener(req, timeout=8):
                pass
        except Exception as e:
            print("NTFY ERROR:", e, flush=True)
