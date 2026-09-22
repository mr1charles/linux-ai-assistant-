"""
remote_bridge.py — lets your phone talk to Little Toby on your laptop.

The phone app (mobile/) is a small installable web app. This file is the
laptop half: a tiny HTTP server inside Toby that serves the app, accepts
requests from it, and reports back what Toby is doing — the live task list,
the reply, and any permission prompt waiting for an answer.

How it's reached from anywhere
------------------------------
The server only ever listens on 127.0.0.1. It is published to your phone by
Tailscale (`tailscale serve`), a free private network between your own
devices: the phone reaches https://<laptop>.<tailnet>.ts.net from any
internet connection, with a real HTTPS certificate, and nobody outside your
own tailnet can reach it at all. Nothing here is ever exposed to the public
internet, and there is no third-party relay holding your requests.

How it's protected
------------------
Being on your tailnet isn't enough on its own. Every API request must carry
a pairing token — 256 random bits, generated on the laptop, handed to the
phone once by QR code, compared in constant time. Repeated wrong tokens from
an address get that address locked out for a minute. Request bodies are
capped in size, and responses carry headers that stop the page being framed
or sniffed. And a paired phone gets no more power than you have sitting at
the laptop: mouse, keyboard and install requests still stop at the same
one-time permission prompt, which the phone can answer only because it's
you holding it.

It is off until you turn it on (`toby phone on`), and `toby phone reset`
throws the token away, unpairing every phone at once.
"""

import hmac
import json
import mimetypes
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAIRING_PATH = Path.home() / "linux-agent" / "remote.json"
MOBILE_DIR = Path(__file__).resolve().parent.parent / "mobile"
MAX_BODY = 16 * 1024
MAX_TEXT = 2000
LONG_POLL_S = 20
LOCKOUT_FAILURES = 8
LOCKOUT_WINDOW_S = 60

STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.css": "app.css",
    "/app.js": "app.js",
    "/sw.js": "sw.js",
    "/manifest.webmanifest": "manifest.webmanifest",
    "/icon.svg": "icon.svg",
}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data:; "
                                "style-src 'self'; script-src 'self'; connect-src 'self'; "
                                "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"),
    "Permissions-Policy": "camera=(), geolocation=(), microphone=(self)",
}


# ---------------------------------------------------------------------------
# Pairing token
# ---------------------------------------------------------------------------

def load_token(path=PAIRING_PATH, create=True):
    """The pairing token, making one the first time it's needed."""
    try:
        data = json.loads(path.read_text())
        token = data.get("token", "")
        if isinstance(token, str) and len(token) >= 32:
            return token
    except (OSError, ValueError):
        pass
    if not create:
        return None
    return reset_token(path)


def reset_token(path=PAIRING_PATH):
    """A fresh token; every previously paired phone stops working."""
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    # written readable by you alone, before it has anything in it
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"token": token, "created": int(time.time())}, f)
    os.replace(tmp, path)
    return token


def pairing_url(base, token):
    """The link the QR code carries.

    The token rides in the #fragment, which browsers never send to any
    server and never write to logs; the app reads it once, stores it, and
    wipes it from the address bar.
    """
    return f"{base.rstrip('/')}/#pair={token}"


# ---------------------------------------------------------------------------
# Shared state the phone reads
# ---------------------------------------------------------------------------

class StateBoard:
    """The latest snapshot of what Toby is doing, with long-poll waiting.

    The GTK thread publishes; request threads wait for something newer than
    what their phone already has. A phone asking "anything since version 41?"
    is answered the instant version 42 appears, or after LONG_POLL_S with
    nothing new — so the phone updates immediately without hammering the
    laptop with requests.
    """

    def __init__(self):
        self._cond = threading.Condition()
        self._version = 0
        self._state = {"busy": False, "steps": [], "reply": "", "task": "",
                       "confirm": None, "history": []}

    def publish(self, state):
        with self._cond:
            self._version += 1
            self._state = dict(state)
            self._cond.notify_all()

    def snapshot(self, since=None, timeout=LONG_POLL_S):
        with self._cond:
            if since is not None and since >= self._version:
                self._cond.wait_for(lambda: self._version > since, timeout=timeout)
            return {"version": self._version, **self._state}


class FailureTracker:
    """Locks an address out after too many wrong tokens in a short time."""

    def __init__(self, limit=LOCKOUT_FAILURES, window=LOCKOUT_WINDOW_S, clock=time.monotonic):
        self.limit = limit
        self.window = window
        self.clock = clock
        self._fails = {}
        self._lock = threading.Lock()

    def locked(self, addr):
        now = self.clock()
        with self._lock:
            recent = [t for t in self._fails.get(addr, []) if now - t < self.window]
            self._fails[addr] = recent
            return len(recent) >= self.limit

    def fail(self, addr):
        with self._lock:
            self._fails.setdefault(addr, []).append(self.clock())


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------

class RemoteBridge:
    """Runs the server on a background thread.

    callbacks must provide ask(text) -> (ok, message), confirm(answer) ->
    bool and cancel() -> bool. They're called on request threads; the app
    marshals them onto GTK itself.
    """

    def __init__(self, callbacks, token, host="127.0.0.1", port=8765,
                 mobile_dir=MOBILE_DIR, board=None):
        self.callbacks = callbacks
        self.token = token
        self.host = host
        self.port = port
        self.mobile_dir = Path(mobile_dir)
        self.board = board or StateBoard()
        self.failures = FailureTracker()
        self._server = None
        self._thread = None

    @property
    def address(self):
        return self._server.server_address if self._server else None

    def start(self):
        bridge = self

        class Handler(_Handler):
            pass

        Handler.bridge = bridge
        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True, name="remote-bridge")
        self._thread.start()
        return self

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def publish(self, state):
        self.board.publish(state)

    def authorized(self, header):
        if not header or not header.startswith("Bearer "):
            return False
        supplied = header[len("Bearer "):].strip()
        return hmac.compare_digest(supplied.encode(), self.token.encode())


class _Handler(BaseHTTPRequestHandler):
    bridge = None
    server_version = "LittleToby"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass  # requests carry what you asked Toby to do; keep them out of logs

    # -- helpers --------------------------------------------------------------------
    def _send(self, status, body=b"", content_type="application/json", extra=None):
        self.send_response(status)
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj).encode(), extra={"Cache-Control": "no-store"})

    def _client(self):
        # Behind `tailscale serve` everything arrives from loopback, so the
        # tailnet peer is the more useful identity when it's present.
        return self.headers.get("Tailscale-User-Login") or self.client_address[0]

    def _check_auth(self):
        client = self._client()
        if self.bridge.failures.locked(client):
            self._json(429, {"error": "Too many wrong pairing codes. Wait a minute."})
            return False
        if not self.bridge.authorized(self.headers.get("Authorization", "")):
            self.bridge.failures.fail(client)
            self._json(401, {"error": "This phone isn't paired. Scan the code from `toby phone pair`."})
            return False
        return True

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length < 0 or length > MAX_BODY:
            return None
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    # -- routes -------------------------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in STATIC_FILES:
            return self._static(STATIC_FILES[path])
        if path == "/api/state":
            if not self._check_auth():
                return
            since = None
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            for part in query.split("&"):
                if part.startswith("since="):
                    try:
                        since = int(part[6:])
                    except ValueError:
                        since = None
            return self._json(200, self.bridge.board.snapshot(since))
        if path == "/api/ping":
            if not self._check_auth():
                return
            return self._json(200, {"ok": True})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if not self._check_auth():
            return
        body = self._body()
        if body is None:
            return self._json(400, {"error": "bad request"})
        cb = self.bridge.callbacks
        if path == "/api/ask":
            text = str(body.get("text", "")).strip()
            if not text:
                return self._json(400, {"error": "say something first"})
            if len(text) > MAX_TEXT:
                return self._json(413, {"error": "that's too long"})
            ok, message = cb.ask(text)
            return self._json(200 if ok else 409, {"ok": ok, "message": message})
        if path == "/api/confirm":
            answer = body.get("answer")
            if not isinstance(answer, bool):
                return self._json(400, {"error": "answer must be true or false"})
            return self._json(200, {"ok": bool(cb.confirm(answer))})
        if path == "/api/cancel":
            return self._json(200, {"ok": bool(cb.cancel())})
        self._json(404, {"error": "not found"})

    def _static(self, name):
        file_path = (self.bridge.mobile_dir / name).resolve()
        if file_path.parent != self.bridge.mobile_dir.resolve() or not file_path.is_file():
            return self._json(404, {"error": "not found"})
        body = file_path.read_bytes()
        ctype = {".webmanifest": "application/manifest+json",
                 ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".html": "text/html; charset=utf-8",
                 ".svg": "image/svg+xml"}.get(file_path.suffix,
                                              mimetypes.guess_type(str(file_path))[0] or "application/octet-stream")
        extra = {"Cache-Control": "no-cache"}
        if name == "sw.js":
            extra["Service-Worker-Allowed"] = "/"
        self._send(200, body, ctype, extra)
