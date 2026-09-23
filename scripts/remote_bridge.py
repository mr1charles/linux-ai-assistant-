"""
remote_bridge.py — lets your phone talk to Little Toby on your computer.

This is the computer half of the companion apps: a small HTTP server inside
Toby that the iPhone app and the web app (mobile/) both talk to. It accepts
requests, reports back what Toby is doing (the live task, its steps, any
permission prompt waiting for an answer, background jobs), and serves the
computer's status, an optional view of the screen, and notifications.

How it's reached from anywhere
------------------------------
The server only ever listens on 127.0.0.1. It is published to your phone by
Tailscale (`tailscale serve`), a free private network between your own
devices: the phone reaches https://<computer>.<tailnet>.ts.net from any
internet connection, over WireGuard, with a real HTTPS certificate on top,
and nobody outside your own tailnet can reach it at all. When the phone and
computer can't connect directly, Tailscale relays the (still end-to-end
encrypted) traffic. Nothing here is ever exposed to the public internet.

How it's protected
------------------
Being on your tailnet isn't enough on its own. Each phone pairs separately:

1. On the computer, "Connect a phone" (or `toby phone pair`) starts a pairing
   session with a one-time code, valid for five minutes, shown as a QR code
   and as eight characters you can type.
2. The phone sends that code with its name and a random device id.
3. Both screens show the same six-digit number, derived from the code and the
   phone's id, so you can see it's your phone asking.
4. You approve on the computer. Only the computer can approve a pairing —
   never another phone.
5. The phone receives its own 256-bit token, once. The computer keeps only a
   SHA-256 of it.

Every API request carries that token, compared in constant time. Repeated
wrong tokens or pairing codes from an address lock it out for a minute.
Each phone can be revoked on its own, can log itself out, and `toby phone
reset` unpairs them all. Request bodies are size-capped, and responses
carry headers that stop pages being framed or sniffed.

A paired phone gets no more power than you have sitting at the computer:
everything Toby does still goes through the same permission levels (see
permissions.py), and anything that needs approval asks on the phone and the
computer at once.

It is off until you turn it on (`toby phone on`).
"""

import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import socket
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAIRING_PATH = Path.home() / "linux-agent" / "remote.json"
MOBILE_DIR = Path(__file__).resolve().parent.parent / "mobile"
API_VERSION = 2
MAX_BODY = 16 * 1024
MAX_TEXT = 2000
LONG_POLL_S = 20
LOCKOUT_FAILURES = 8
LOCKOUT_WINDOW_S = 60
PAIRING_TTL_S = 300
TOKEN_DELIVERY_TTL_S = 300
LAST_SEEN_WRITE_S = 60

# Pairing codes avoid characters that are easy to misread: no 0/O, 1/I/L.
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 8

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
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data: blob:; "
                                "style-src 'self'; script-src 'self'; connect-src 'self'; "
                                "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"),
    "Permissions-Policy": "camera=(self), geolocation=(), microphone=(self)",
}


def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def compare_code(code, device_id):
    """The six digits both screens show while pairing.

    Derived from the pairing code and the phone's id, so the computer and
    the phone each work it out on their own; if they match, the request on
    the computer's screen is from the phone in your hand.
    """
    digest = hmac.new(code.encode(), device_id.encode(), hashlib.sha256).digest()
    return f"{int.from_bytes(digest[:4], 'big') % 1_000_000:06d}"


def normalize_code(text):
    return "".join(ch for ch in str(text).upper() if ch in CODE_ALPHABET)


def new_code():
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def format_code(code):
    return f"{code[:4]}-{code[4:]}" if len(code) == 8 else code


def computer_name(settings=None):
    name = (settings or {}).get("computer_name", "").strip()
    if name:
        return name
    try:
        pretty = subprocess.run(["hostnamectl", "--pretty"], capture_output=True, text=True,
                                timeout=2).stdout.strip()
        if pretty:
            return pretty
    except (OSError, subprocess.TimeoutExpired):
        pass
    return socket.gethostname() or "My Computer"


def tailscale_name():
    """This computer's name on your tailnet, if Tailscale is up."""
    try:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True,
                             text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode:
        return None
    try:
        name = json.loads(out.stdout).get("Self", {}).get("DNSName", "")
    except ValueError:
        return None
    return name.rstrip(".") or None


def pairing_url(base, code):
    """The link a pairing QR code carries.

    The code rides in the #fragment, which browsers never send to any server
    and never write to logs. The iPhone app's scanner reads the same link;
    scanned with the ordinary camera, it opens the web app instead.
    """
    return f"{base.rstrip('/')}/#pair={code}"


# ---------------------------------------------------------------------------
# Paired devices, on disk
# ---------------------------------------------------------------------------

class DeviceStore:
    """The paired phones, kept in remote.json (readable by you alone).

    Only a SHA-256 of each phone's token is stored; the token itself exists
    on the phone and nowhere else. The file is re-read when it changes, so
    `toby phone reset` in a terminal takes effect in the running app at once.

    A remote.json from before per-device pairing held one shared token. That
    becomes a single device called "Phone paired with a link", so a phone
    already set up keeps working until you revoke it.
    """

    def __init__(self, path=None):
        self.path = Path(path or PAIRING_PATH)
        self._lock = threading.RLock()
        self._mtime = None
        self._data = None
        self._last_write = {}
        self._load(force=True)

    # -- file -----------------------------------------------------------------
    def _load(self, force=False):
        with self._lock:
            try:
                mtime = self.path.stat().st_mtime_ns
            except OSError:
                mtime = None
            if not force and mtime == self._mtime and self._data is not None:
                return self._data
            try:
                data = json.loads(self.path.read_text())
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}
            changed = False
            if data.get("version") != 2:
                devices = []
                legacy = data.get("token")
                if isinstance(legacy, str) and len(legacy) >= 32:
                    devices.append({"id": "legacy-link", "name": "Phone paired with a link",
                                    "platform": "web", "token_sha256": _hash_token(legacy),
                                    "paired_at": int(data.get("created", time.time())),
                                    "last_seen": None})
                data = {"version": 2, "devices": devices}
                changed = True
            if not data.get("computer_id"):
                data["computer_id"] = str(uuid.uuid4())
                changed = True
            if not data.get("admin_key"):
                data["admin_key"] = secrets.token_urlsafe(32)
                changed = True
            data.setdefault("devices", [])
            self._data = data
            if changed:
                self._write()
            else:
                self._mtime = mtime
            return data

    def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        # written readable by you alone, before it has anything in it
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self._data, f, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        self._mtime = self.path.stat().st_mtime_ns

    # -- queries --------------------------------------------------------------
    @property
    def computer_id(self):
        return self._load()["computer_id"]

    @property
    def admin_key(self):
        return self._load()["admin_key"]

    def devices(self):
        return [self._public(d) for d in self._load()["devices"]]

    @staticmethod
    def _public(device):
        return {k: device.get(k) for k in ("id", "name", "platform", "paired_at", "last_seen")}

    def authenticate(self, token):
        """The device a token belongs to, or None. Constant time per device."""
        if not token or len(token) > 200:
            return None
        supplied = _hash_token(token)
        found = None
        for device in self._load()["devices"]:
            if hmac.compare_digest(supplied, device.get("token_sha256", "")):
                found = device
        return self._public(found) if found else None

    # -- changes --------------------------------------------------------------
    def add(self, name, platform, device_id=None):
        """Pair a new device. Returns (device, token); the token is shown once."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            data = self._load()
            device_id = device_id or str(uuid.uuid4())
            data["devices"] = [d for d in data["devices"] if d.get("id") != device_id]
            device = {"id": device_id, "name": str(name)[:60] or "Phone",
                      "platform": str(platform)[:12] or "phone",
                      "token_sha256": _hash_token(token),
                      "paired_at": int(time.time()), "last_seen": None}
            data["devices"].append(device)
            self._write()
        return self._public(device), token

    def revoke(self, device_id):
        with self._lock:
            data = self._load()
            before = len(data["devices"])
            data["devices"] = [d for d in data["devices"] if d.get("id") != device_id]
            if len(data["devices"]) == before:
                return False
            self._write()
            return True

    def reset(self):
        """Unpair every phone."""
        with self._lock:
            self._load()["devices"] = []
            self._write()

    def touch(self, device_id):
        """Note that a device was just used (written at most once a minute)."""
        now = time.time()
        if now - self._last_write.get(device_id, 0) < LAST_SEEN_WRITE_S:
            return
        with self._lock:
            for device in self._load()["devices"]:
                if device.get("id") == device_id:
                    device["last_seen"] = int(now)
                    self._last_write[device_id] = now
                    self._write()
                    return


# Kept for callers from before per-device pairing: an existing token still
# works (as the "Phone paired with a link" device) and reset unpairs all.
def reset_token(path=None):
    DeviceStore(path).reset()


# ---------------------------------------------------------------------------
# Pairing sessions, in memory
# ---------------------------------------------------------------------------

class PairingCenter:
    """One-time pairing sessions and the phones claiming them.

    Nothing about a session is written to disk: if Toby restarts mid-pairing,
    you simply start again. on_claim(claim) is called when a phone presents a
    valid code, so the computer can ask you; decide() records your answer.
    """

    def __init__(self, store, clock=time.monotonic, on_claim=None):
        self.store = store
        self.clock = clock
        self.on_claim = on_claim
        self._cond = threading.Condition()
        self._sessions = {}   # session id -> session
        self._claims = {}     # claim id -> claim

    def start(self, base_url=""):
        """A new session. Any older unused session is cancelled."""
        with self._cond:
            self._expire()
            for s in self._sessions.values():
                s["cancelled"] = True
            code = new_code()
            session = {"id": secrets.token_hex(8), "code": code,
                       "expires": self.clock() + PAIRING_TTL_S, "claimed": False,
                       "cancelled": False}
            self._sessions[session["id"]] = session
        return {"id": session["id"], "code": code, "display_code": format_code(code),
                "url": pairing_url(base_url, code) if base_url else "",
                "expires_in": PAIRING_TTL_S}

    def cancel(self, session_id=None):
        with self._cond:
            for s in self._sessions.values():
                if session_id is None or s["id"] == session_id:
                    s["cancelled"] = True
            self._cond.notify_all()

    def _expire(self):
        now = self.clock()
        for key in [k for k, s in self._sessions.items() if now > s["expires"] + 60]:
            del self._sessions[key]
        for key in [k for k, c in self._claims.items() if now > c["expires"] + 60]:
            del self._claims[key]

    def claim(self, code, device_name, device_id, platform):
        """A phone presents a code. Returns (claim, None) or (None, error)."""
        code = normalize_code(code)
        device_id = str(device_id or "")[:64]
        if not device_id or len(device_id) < 8:
            return None, "missing device id"
        with self._cond:
            self._expire()
            now = self.clock()
            match = None
            for s in self._sessions.values():
                if hmac.compare_digest(s["code"], code) and len(code) == CODE_LENGTH:
                    match = s
            if match is None or match["cancelled"] or match["claimed"] or now > match["expires"]:
                return None, "That pairing code isn't valid. Start pairing again on the computer."
            match["claimed"] = True
            claim = {"id": secrets.token_urlsafe(24), "session": match["id"],
                     "device_id": device_id, "name": str(device_name or "Phone")[:60],
                     "platform": str(platform or "phone")[:12],
                     "compare": compare_code(code, device_id), "status": "pending",
                     "token": None, "device": None, "expires": now + PAIRING_TTL_S}
            self._claims[claim["id"]] = claim
            self._cond.notify_all()
        if self.on_claim:
            try:
                self.on_claim(dict(claim))
            except Exception as e:  # the prompt failing must not break pairing
                print("PAIRING PROMPT ERROR:", e, flush=True)
        return claim, None

    def pending_claims(self, session_id=None):
        with self._cond:
            return [dict(c, token=None) for c in self._claims.values()
                    if c["status"] == "pending" and (session_id is None or c["session"] == session_id)]

    def wait_for_claim(self, session_id, timeout):
        with self._cond:
            self._cond.wait_for(lambda: self.pending_claims(session_id) or not self._session_live(session_id),
                                timeout=timeout)
            return self.pending_claims(session_id)

    def _session_live(self, session_id):
        s = self._sessions.get(session_id)
        return bool(s) and not s["cancelled"] and self.clock() <= s["expires"]

    def decide(self, claim_id, allow):
        """Your answer, on the computer. Pairs the phone if allowed."""
        with self._cond:
            claim = self._claims.get(claim_id)
            if not claim or claim["status"] != "pending":
                return False
            if self.clock() > claim["expires"]:
                claim["status"] = "expired"
                self._cond.notify_all()
                return False
            if allow:
                device, token = self.store.add(claim["name"], claim["platform"], claim["device_id"])
                claim.update(status="approved", token=token, device=device,
                             expires=self.clock() + TOKEN_DELIVERY_TTL_S)
            else:
                claim["status"] = "denied"
            self._cond.notify_all()
            return True

    def wait(self, claim_id, timeout=LONG_POLL_S):
        """What the phone polls. The token is handed over exactly once."""
        with self._cond:
            claim = self._claims.get(claim_id)
            if claim is None:
                return {"status": "unknown"}
            self._cond.wait_for(lambda: claim["status"] != "pending" or self.clock() > claim["expires"],
                                timeout=timeout)
            if claim["status"] == "pending" and self.clock() > claim["expires"]:
                claim["status"] = "expired"
            result = {"status": claim["status"], "compare": claim["compare"]}
            if claim["status"] == "approved" and claim["token"]:
                result.update(token=claim["token"], device=claim["device"])
                claim["token"] = None   # never again
            return result


# ---------------------------------------------------------------------------
# Shared state the phone reads
# ---------------------------------------------------------------------------

class StateBoard:
    """The latest snapshot of what Toby is doing, with long-poll waiting.

    The app publishes; request threads wait for something newer than what
    their phone already has. A phone asking "anything since version 41?" is
    answered the instant version 42 appears, or after LONG_POLL_S with
    nothing new — so the phone updates immediately without hammering the
    computer with requests.
    """

    def __init__(self):
        self._cond = threading.Condition()
        self._version = 0
        self._state = {"busy": False, "steps": [], "reply": "", "task": None,
                       "confirm": None, "approvals": [], "history": [], "jobs": []}

    @property
    def version(self):
        return self._version

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


class EventLog:
    """Things worth a notification on the phone, with long-poll waiting.

    Deliberately stingy: an identical event within a minute is dropped, and
    there's an hourly cap, so a flapping job can't bury your lock screen.
    listeners (e.g. the optional ntfy sender) are told about each one kept.
    """

    MAX_PER_HOUR = 30

    def __init__(self, clock=time.time):
        self.clock = clock
        self._cond = threading.Condition()
        self._events = []
        self._next = 1
        self.listeners = []

    def add(self, kind, title, body=""):
        now = self.clock()
        with self._cond:
            recent = [e for e in self._events if now - e["at"] < 3600]
            if any(e["kind"] == kind and e["title"] == title and e["body"] == body
                   and now - e["at"] < 60 for e in recent):
                return None
            if len(recent) >= self.MAX_PER_HOUR:
                return None
            event = {"id": self._next, "kind": kind, "title": str(title)[:120],
                     "body": str(body)[:300], "at": now}
            self._next += 1
            self._events = (recent + [event])[-100:]
            self._cond.notify_all()
        for listener in list(self.listeners):
            try:
                listener(event)
            except Exception as e:
                print("NOTIFY ERROR:", e, flush=True)
        return event

    def since(self, after, timeout=0):
        with self._cond:
            if timeout:
                self._cond.wait_for(lambda: any(e["id"] > after for e in self._events), timeout=timeout)
            return [e for e in self._events if e["id"] > after]

    @property
    def last_id(self):
        with self._cond:
            return self._next - 1


class FailureTracker:
    """Locks an address out after too many wrong tokens or codes."""

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

    services is the app's side of the API. Everything is optional except
    ask(); a missing method answers "not available on this computer" rather
    than pretending. Methods are called on request threads, and the app
    marshals any GTK work onto its own thread:

      ask(text, device) -> (ok, message, task_id)
      task_control(action, device) -> (ok, message)      pause | resume | stop
      approve(approval_id, allow, device) -> bool
      status() -> dict                                    real measurements only
      tasks() -> list;  task(task_id) -> dict | None
      jobs() -> list;   job(job_id, lines) -> dict | None
      screen(view, max_width) -> (jpeg bytes | None, reason)
      overview() -> dict;  files() -> list
      focus_window(address, device) -> (ok, message)
      set_work_mode(on, device) -> (ok, message)
      device_revoked(device) -> None
    """

    def __init__(self, services, store=None, host="127.0.0.1", port=8765,
                 mobile_dir=MOBILE_DIR, board=None, events=None, name="My Computer",
                 pairing=None):
        self.services = services
        self.store = store if store is not None else DeviceStore()
        self.pairing = pairing or PairingCenter(self.store)
        self.host = host
        self.port = port
        self.name = name
        self.mobile_dir = Path(mobile_dir)
        self.board = board or StateBoard()
        self.events = events or EventLog()
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

    def computer(self):
        return {"id": self.store.computer_id, "name": self.name, "api": API_VERSION}


def _query(path):
    params = {}
    if "?" in path:
        for part in path.split("?", 1)[1].split("&"):
            if "=" in part:
                key, value = part.split("=", 1)
                params[key] = value
    return params


def _int(value, default=None, low=None, high=None):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if low is not None:
        number = max(low, number)
    if high is not None:
        number = min(high, number)
    return number


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

    def _device(self):
        """The paired device making this request, or None (and a reply sent)."""
        client = self._client()
        if self.bridge.failures.locked(client):
            self._json(429, {"error": "Too many wrong attempts. Wait a minute.", "code": "locked"})
            return None
        header = self.headers.get("Authorization", "")
        token = header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""
        device = self.bridge.store.authenticate(token)
        if device is None:
            self.bridge.failures.fail(client)
            self._json(401, {"error": "This phone isn't paired with this computer. Pair it again.",
                             "code": "unpaired"})
            return None
        self.bridge.store.touch(device["id"])
        return device

    def _admin(self):
        """The computer's own tools (`toby phone pair`), on loopback only."""
        through_proxy = any(h.lower().startswith("tailscale-") for h in self.headers.keys())
        supplied = self.headers.get("X-Toby-Admin", "")
        if (through_proxy or self.client_address[0] != "127.0.0.1" or not supplied
                or not hmac.compare_digest(supplied.encode(), self.bridge.store.admin_key.encode())):
            self._json(403, {"error": "forbidden"})
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

    def _service(self, name):
        fn = getattr(self.bridge.services, name, None)
        if fn is None:
            self._json(501, {"error": "That isn't available on this computer yet.",
                             "code": "unavailable"})
        return fn

    # -- routes -------------------------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        params = _query(self.path)
        if path in STATIC_FILES:
            return self._static(STATIC_FILES[path])
        if path == "/api/hello":
            # Unauthenticated, and says only what a pairing screen needs.
            return self._json(200, {"app": "Little Toby", **self.bridge.computer()})
        if path == "/api/pair/wait":
            claim_id = params.get("claim", "")
            return self._json(200, self.bridge.pairing.wait(claim_id, timeout=LONG_POLL_S))
        if path.startswith("/api/admin/"):
            return self._admin_get(path, params)
        if not path.startswith("/api/"):
            return self._json(404, {"error": "not found"})

        device = self._device()
        if device is None:
            return
        b, s = self.bridge, self.bridge.services
        if path == "/api/state":
            since = _int(params.get("since"))
            state = b.board.snapshot(since)
            state["computer"] = b.computer()
            state["device"] = device
            state["events_last"] = b.events.last_id
            return self._json(200, state)
        if path == "/api/ping":
            return self._json(200, {"ok": True, "computer": b.computer()})
        if path == "/api/status":
            fn = self._service("status")
            return fn and self._json(200, {"computer": b.computer(), **fn()})
        if path == "/api/tasks":
            fn = self._service("tasks")
            return fn and self._json(200, {"tasks": fn()})
        if path.startswith("/api/tasks/"):
            fn = self._service("task")
            if fn:
                task = fn(path[len("/api/tasks/"):])
                return self._json(200 if task else 404, task or {"error": "no such task"})
            return
        if path == "/api/jobs":
            fn = self._service("jobs")
            return fn and self._json(200, {"jobs": fn()})
        if path.startswith("/api/jobs/"):
            fn = self._service("job")
            if fn:
                job = fn(path[len("/api/jobs/"):], _int(params.get("lines"), 200, 1, 2000))
                return self._json(200 if job else 404, job or {"error": "no such job"})
            return
        if path == "/api/screen":
            fn = self._service("screen")
            if not fn:
                return
            view = params.get("view", "full")
            if view not in ("full", "toby"):
                return self._json(400, {"error": "view must be full or toby"})
            image, reason = fn(view, _int(params.get("max"), 1170, 200, 2400))
            if image is None:
                return self._json(403 if reason == "screen_view_off" else 503,
                                  {"error": reason or "unavailable", "code": reason or "unavailable"})
            return self._send(200, image, "image/jpeg", {"Cache-Control": "no-store"})
        if path == "/api/overview":
            fn = self._service("overview")
            return fn and self._json(200, fn())
        if path == "/api/files":
            fn = self._service("files")
            return fn and self._json(200, {"files": fn()})
        if path == "/api/events":
            after = _int(params.get("after"), 0)
            wait = _int(params.get("wait"), 0, 0, LONG_POLL_S)
            return self._json(200, {"events": b.events.since(after, timeout=wait),
                                    "last": b.events.last_id})
        if path == "/api/devices":
            devices = [dict(d, this_device=(d["id"] == device["id"])) for d in b.store.devices()]
            return self._json(200, {"devices": devices})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        body = self._body()
        if body is None:
            return self._json(400, {"error": "bad request"})
        if path == "/api/pair/claim":
            return self._pair_claim(body)
        if path.startswith("/api/admin/"):
            return self._admin_post(path, body)
        device = self._device()
        if device is None:
            return
        b, s = self.bridge, self.bridge.services
        if path == "/api/ask":
            text = str(body.get("text", "")).strip()
            if not text:
                return self._json(400, {"error": "say something first"})
            if len(text) > MAX_TEXT:
                return self._json(413, {"error": "that's too long"})
            ok, message, task_id = s.ask(text, device)
            return self._json(200 if ok else 409, {"ok": ok, "message": message, "task_id": task_id})
        if path in ("/api/task/pause", "/api/task/resume", "/api/task/stop", "/api/cancel"):
            action = "stop" if path == "/api/cancel" else path.rsplit("/", 1)[1]
            fn = self._service("task_control")
            if fn:
                ok, message = fn(action, device)
                return self._json(200 if ok else 409, {"ok": ok, "message": message})
            return
        if path == "/api/approve":
            allow = body.get("allow")
            if not isinstance(allow, bool) or not isinstance(body.get("id"), str):
                return self._json(400, {"error": "need an approval id and allow true or false"})
            fn = self._service("approve")
            return fn and self._json(200, {"ok": bool(fn(body["id"], allow, device))})
        if path == "/api/confirm":
            # the web app's original answer-the-prompt call
            answer = body.get("answer")
            if not isinstance(answer, bool):
                return self._json(400, {"error": "answer must be true or false"})
            fn = self._service("approve")
            if fn:
                pending = b.board.snapshot().get("approvals") or []
                ok = bool(pending) and bool(fn(pending[0]["id"], answer, device))
                return self._json(200, {"ok": ok})
            return
        if path == "/api/window/focus":
            fn = self._service("focus_window")
            if fn:
                ok, message = fn(str(body.get("address", ""))[:40], device)
                return self._json(200 if ok else 409, {"ok": ok, "message": message})
            return
        if path == "/api/work_mode":
            on = body.get("on")
            if not isinstance(on, bool):
                return self._json(400, {"error": "on must be true or false"})
            fn = self._service("set_work_mode")
            if fn:
                ok, message = fn(on, device)
                return self._json(200 if ok else 409, {"ok": ok, "message": message})
            return
        if path == "/api/devices/revoke":
            target = str(body.get("id", ""))
            ok = b.store.revoke(target)
            if ok:
                b.events.add("device", "A phone was unpaired",
                             f"{device['name']} unpaired a device." if target != device["id"]
                             else f"{device['name']} logged out.")
                if hasattr(s, "device_revoked"):
                    s.device_revoked({"id": target})
            return self._json(200 if ok else 404, {"ok": ok})
        if path == "/api/logout":
            ok = b.store.revoke(device["id"])
            if ok and hasattr(s, "device_revoked"):
                s.device_revoked(device)
            return self._json(200, {"ok": ok})
        self._json(404, {"error": "not found"})

    # -- pairing ----------------------------------------------------------------------------
    def _pair_claim(self, body):
        client = self._client()
        if self.bridge.failures.locked(client):
            return self._json(429, {"error": "Too many wrong codes. Wait a minute.", "code": "locked"})
        claim, error = self.bridge.pairing.claim(body.get("code", ""), body.get("device_name", ""),
                                                 body.get("device_id", ""), body.get("platform", ""))
        if claim is None:
            self.bridge.failures.fail(client)
            return self._json(403, {"error": error, "code": "bad_code"})
        return self._json(200, {"claim": claim["id"], "compare": claim["compare"],
                                "computer": self.bridge.computer(),
                                "expires_in": PAIRING_TTL_S})

    def _admin_get(self, path, params):
        if not self._admin():
            return
        pairing = self.bridge.pairing
        if path == "/api/admin/pair/claims":
            claims = pairing.wait_for_claim(params.get("session", ""), timeout=_int(params.get("wait"), 0, 0, 30))
            return self._json(200, {"claims": [{k: c[k] for k in ("id", "name", "platform", "compare")}
                                               for c in claims]})
        if path == "/api/admin/devices":
            return self._json(200, {"devices": self.bridge.store.devices()})
        self._json(404, {"error": "not found"})

    def _admin_post(self, path, body):
        if not self._admin():
            return
        pairing = self.bridge.pairing
        if path == "/api/admin/pair/start":
            return self._json(200, pairing.start(str(body.get("base_url", ""))[:200]))
        if path == "/api/admin/pair/decide":
            ok = pairing.decide(str(body.get("claim", "")), bool(body.get("allow")))
            return self._json(200 if ok else 404, {"ok": ok})
        if path == "/api/admin/pair/cancel":
            pairing.cancel(body.get("session"))
            return self._json(200, {"ok": True})
        if path == "/api/admin/devices/revoke":
            return self._json(200, {"ok": self.bridge.store.revoke(str(body.get("id", "")))})
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


# ---------------------------------------------------------------------------
# The computer's own client for the admin routes (used by `toby phone`)
# ---------------------------------------------------------------------------

def admin_call(path, body=None, port=8765, store=None, timeout=35):
    """Call the running Toby's admin routes. Returns (status, data)."""
    import urllib.error
    import urllib.request
    store = store or DeviceStore()
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 method="POST" if body is not None else "GET")
    req.add_header("X-Toby-Admin", store.admin_key)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (OSError, ValueError) as e:
        return 0, {"error": str(e)}
