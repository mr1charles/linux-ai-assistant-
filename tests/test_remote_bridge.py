"""The phone bridge, over real HTTP on a loopback port.

The things that must hold: nothing under /api works without the token;
guessing gets you locked out; a wrong-length or malformed token can't slip
through; oversized bodies are refused; the static app is served without
leaking anything; and the long-poll answers as soon as state changes.
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import remote_bridge as rb  # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


# -- the token --------------------------------------------------------------------
workdir = Path(tempfile.mkdtemp())
token_file = workdir / "remote.json"
token = rb.load_token(token_file)
check("a token is created on first use", len(token) >= 40, True)
check("and kept", rb.load_token(token_file), token)
check("and only readable by you", oct(token_file.stat().st_mode & 0o777), "0o600")
fresh = rb.reset_token(token_file)
check("resetting makes a different one", fresh != token, True)
token = fresh
check("the pairing link carries it in the fragment",
      rb.pairing_url("https://toby.tail.ts.net", token), f"https://toby.tail.ts.net/#pair={token}")


# -- a running bridge -----------------------------------------------------------------
class Callbacks:
    def __init__(self):
        self.asked, self.confirmed, self.cancelled = [], [], 0
        self.busy = False

    def ask(self, text):
        if self.busy:
            return False, "busy"
        self.asked.append(text)
        return True, "Sent."

    def confirm(self, answer):
        self.confirmed.append(answer)
        return True

    def cancel(self):
        self.cancelled += 1
        return True


cb = Callbacks()
bridge = rb.RemoteBridge(cb, token, port=0).start()
host, port = bridge.address
base = f"http://{host}:{port}"
check("it only listens on loopback", host, "127.0.0.1")


def call(path, body=None, auth=token, headers=None):
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = urllib.request.Request(base + path, data=data, method="POST" if data is not None else "GET")
    if auth is not None:
        req.add_header("Authorization", "Bearer " + auth)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


status, body, headers = call("/", auth=None)
check("the app page is served without a token", status, 200)
check("it is the app", b"Little Toby" in body, True)
check("framing is refused", headers.get("X-Frame-Options"), "DENY")
check("a content security policy is set", "default-src 'self'" in headers.get("Content-Security-Policy", ""), True)
check("no token appears in the page", token.encode() in body, False)
for asset in ("/app.js", "/app.css", "/sw.js", "/manifest.webmanifest", "/icon.svg"):
    check(f"{asset} is served", call(asset, auth=None)[0], 200)
check("paths outside the app aren't", call("/../scripts/remote_bridge.py", auth=None)[0], 404)
check("nor anything else", call("/etc/passwd", auth=None)[0], 404)

check("state needs the token", call("/api/state", auth=None)[0], 401)
check("a wrong token is refused", call("/api/state", auth="x" * len(token))[0], 401)
check("a prefix of the right token is refused", call("/api/state", auth=token[:-1])[0], 401)
check("the right token with junk after is refused", call("/api/state", auth=token + "A")[0], 401)
check("asking needs the token", call("/api/ask", {"text": "open youtube"}, auth=None)[0], 401)
check("and nothing got through", cb.asked, [])
check("the right token works", call("/api/state")[0], 200)

status, body, _ = call("/api/ask", {"text": "open youtube"})
check("a request is accepted", (status, json.loads(body)["ok"]), (200, True))
check("and reaches Toby", cb.asked, ["open youtube"])
check("empty text is refused", call("/api/ask", {"text": "   "})[0], 400)
check("very long text is refused", call("/api/ask", {"text": "a" * 5000})[0], 413)
check("an oversized body is refused", call("/api/ask", b"{" + b" " * (rb.MAX_BODY + 10) + b"}")[0], 400)
check("junk JSON is refused", call("/api/ask", b"not json")[0], 400)
check("a non-object body is refused", call("/api/ask", b"[1,2]")[0], 400)
cb.busy = True
check("while busy, a second request says so", call("/api/ask", {"text": "x"})[0], 409)
cb.busy = False
check("confirm needs a real true/false", call("/api/confirm", {"answer": "yes"})[0], 400)
call("/api/confirm", {"answer": True})
check("a confirm answer reaches Toby", cb.confirmed, [True])
call("/api/cancel", {})
check("cancel reaches Toby", cb.cancelled, 1)

# -- long polling --------------------------------------------------------------------------
status, body, _ = call("/api/state")
v = json.loads(body)["version"]


def later():
    time.sleep(0.4)
    bridge.publish({"busy": True, "steps": [{"label": "Open YouTube", "status": "current"}],
                    "reply": "", "task": "Open YouTube", "confirm": None, "history": []})


threading.Thread(target=later).start()
started = time.monotonic()
status, body, headers = call(f"/api/state?since={v}")
elapsed = time.monotonic() - started
state = json.loads(body)
check("the long poll returns as soon as something changes", 0.3 < elapsed < 3, True)
check("with the new state", state["steps"][0]["label"], "Open YouTube")
check("and a newer version", state["version"], v + 1)
check("API responses aren't cached", headers.get("Cache-Control"), "no-store")

# -- lockout ---------------------------------------------------------------------------------
bridge2 = rb.RemoteBridge(Callbacks(), token, port=0).start()
host2, port2 = bridge2.address
base_saved, base = base, f"http://{host2}:{port2}"
for _ in range(rb.LOCKOUT_FAILURES):
    call("/api/state", auth="guess" * 10)
check("after repeated wrong tokens, even the right one is locked out for a while",
      call("/api/state")[0], 429)
bridge2.stop()
bridge.stop()

if failures:
    print(f"{len(failures)} PROBLEM(S):")
    for f in failures:
        print("  ", f)
    sys.exit(1)
print("phone bridge checks passed")
