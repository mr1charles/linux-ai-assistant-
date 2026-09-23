"""The phone bridge, over real HTTP on a loopback port.

The things that must hold: pairing needs a live one-time code AND your
approval on the computer; the token is handed over once and only its hash
is kept; nothing under /api works without a paired device's token; each
device can be revoked on its own and can log itself out; guessing tokens or
codes gets you locked out; the computer's admin routes can't be reached
through the Tailscale proxy; oversized bodies are refused; the static app is
served without leaking anything; and the long-poll answers as soon as state
changes.
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


# -- codes ------------------------------------------------------------------------------
code = rb.new_code()
check("pairing codes are 8 characters", len(code), 8)
check("from an alphabet with nothing easy to misread",
      set(code) <= set(rb.CODE_ALPHABET) and not set("01OIL") & set(rb.CODE_ALPHABET), True)
check("typed codes are forgiving about case, dashes and spaces",
      rb.normalize_code(" k7m4-xq2p "), "K7M4XQ2P")
check("shown with a dash", rb.format_code("K7M4XQ2P"), "K7M4-XQ2P")
# This exact vector is also checked by the iPhone app's tests
# (ios/LittleTobyTests/PairingTests.swift), so both sides derive the same
# six digits.
check("the comparison number matches the shared test vector",
      rb.compare_code("K7M4XQ2P", "0F8C2D1E-7A3B-4C5D-9E6F-102938475601"), "068302")
check("and six digits", len(rb.compare_code("ABCDEFGH", "device-1234")), 6)
check("the link carries the code in the fragment",
      rb.pairing_url("https://toby.tail.ts.net", "K7M4XQ2P"), "https://toby.tail.ts.net/#pair=K7M4XQ2P")

# -- the device store --------------------------------------------------------------------
workdir = Path(tempfile.mkdtemp())
store_path = workdir / "remote.json"
store = rb.DeviceStore(store_path)
check("a new store has no devices", store.devices(), [])
check("and is only readable by you", oct(store_path.stat().st_mode & 0o777), "0o600")
device, token = store.add("Test iPhone", "ios", "device-aaaa-1111")
check("adding a device returns it", device["name"], "Test iPhone")
check("with a strong token", len(token) >= 40, True)
check("the token itself is never written down", token in store_path.read_text(), False)
check("the token authenticates", store.authenticate(token)["id"], "device-aaaa-1111")
check("a wrong one doesn't", store.authenticate("x" * len(token)), None)
check("nor a prefix", store.authenticate(token[:-1]), None)
check("nor nothing", store.authenticate(""), None)
other = rb.DeviceStore(store_path)
check("another process sees the same devices", [d["id"] for d in other.devices()], ["device-aaaa-1111"])
other.revoke("device-aaaa-1111")
check("and a revoke there takes effect here at once", store.authenticate(token), None)

# a remote.json from before per-device pairing keeps working
legacy_path = workdir / "legacy.json"
legacy_token = "L" * 43
legacy_path.write_text(json.dumps({"token": legacy_token, "created": 1700000000}))
legacy = rb.DeviceStore(legacy_path)
check("an old shared token becomes one device", [d["id"] for d in legacy.devices()], ["legacy-link"])
check("which still authenticates", legacy.authenticate(legacy_token)["name"], "Phone paired with a link")
check("and the plain token is gone from disk", legacy_token in legacy_path.read_text(), False)
rb.reset_token(legacy_path)
check("reset unpairs it", rb.DeviceStore(legacy_path).authenticate(legacy_token), None)

# -- pairing sessions (no network) --------------------------------------------------------
class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


clock = Clock()
prompts = []
pc = rb.PairingCenter(rb.DeviceStore(workdir / "pc.json"), clock=clock, on_claim=prompts.append)
session = pc.start("https://toby.tail.ts.net")
check("a session has a link with its code", session["url"].endswith("#pair=" + session["code"]), True)
claim, err = pc.claim("WRONGCOD", "Phone", "device-bbbb-2222", "ios")
check("a wrong code is refused", (claim, bool(err)), (None, True))
claim, err = pc.claim(session["code"], "Phone", "short", "ios")
check("a device id is required", claim, None)
claim, err = pc.claim(rb.format_code(session["code"]).lower(), "Jacob's iPhone", "device-bbbb-2222", "ios")
check("the right code, typed loosely, is accepted", err, None)
check("the computer is asked", [p["name"] for p in prompts], ["Jacob's iPhone"])
check("both sides get the same comparison number",
      claim["compare"], rb.compare_code(session["code"], "device-bbbb-2222"))
again, err = pc.claim(session["code"], "Someone else", "device-cccc-3333", "ios")
check("a code works once", again, None)
check("waiting shows pending until you answer", pc.wait(claim["id"], timeout=0)["status"], "pending")
check("no token before approval", "token" in pc.wait(claim["id"], timeout=0), False)
pc.decide(claim["id"], True)
result = pc.wait(claim["id"], timeout=0)
check("approved hands over a token", (result["status"], len(result.get("token", "")) >= 40), ("approved", True))
check("exactly once", "token" in pc.wait(claim["id"], timeout=0), False)
check("the device is paired", pc.store.authenticate(result["token"])["name"], "Jacob's iPhone")

session = pc.start()
claim, _ = pc.claim(session["code"], "Stranger", "device-dddd-4444", "web")
pc.decide(claim["id"], False)
check("a denied phone gets no token", pc.wait(claim["id"], timeout=0), {"status": "denied", "compare": claim["compare"]})

session = pc.start()
clock.now += rb.PAIRING_TTL_S + 1
claim, err = pc.claim(session["code"], "Late", "device-eeee-5555", "ios")
check("an expired code is refused", claim, None)
first = pc.start()
second = pc.start()
claim, _ = pc.claim(first["code"], "Old", "device-ffff-6666", "ios")
check("starting a new session cancels the old one", claim, None)
claim, _ = pc.claim(second["code"], "Slow", "device-gggg-7777", "ios")
clock.now += rb.PAIRING_TTL_S + 1
check("an unanswered claim expires", pc.wait(claim["id"], timeout=0)["status"], "expired")
check("and can't be approved afterwards", pc.decide(claim["id"], True), False)

# -- a running bridge ----------------------------------------------------------------------
class Services:
    def __init__(self):
        self.asked, self.approved, self.controls = [], [], []
        self.busy = False

    def ask(self, text, device):
        if self.busy:
            return False, "busy", None
        self.asked.append((text, device["name"]))
        return True, "Sent.", "task-1"

    def approve(self, approval_id, allow, device):
        self.approved.append((approval_id, allow, device["name"]))
        return True

    def task_control(self, action, device):
        self.controls.append(action)
        return True, "ok"

    def status(self):
        return {"hostname": "test", "cpu_percent": None}

    def screen(self, view, max_width):
        return None, "screen_view_off"


services = Services()
store = rb.DeviceStore(workdir / "bridge.json")
bridge = rb.RemoteBridge(services, store, port=0, name="Test Laptop").start()
host, port = bridge.address
base = f"http://{host}:{port}"
check("it only listens on loopback", host, "127.0.0.1")


def call(path, body=None, auth=None, headers=None):
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


def jcall(*a, **k):
    status, body, _ = call(*a, **k)
    try:
        return status, json.loads(body)
    except ValueError:
        return status, body


status, body, headers = call("/")
check("the web app page is served without a token", status, 200)
check("it is the app", b"Little Toby" in body, True)
check("framing is refused", headers.get("X-Frame-Options"), "DENY")
check("a content security policy is set", "default-src 'self'" in headers.get("Content-Security-Policy", ""), True)
for asset in ("/app.js", "/app.css", "/sw.js", "/manifest.webmanifest", "/icon.svg"):
    check(f"{asset} is served", call(asset)[0], 200)
check("paths outside the app aren't", call("/../scripts/remote_bridge.py")[0], 404)
check("nor anything else", call("/etc/passwd")[0], 404)

status, hello = jcall("/api/hello")
check("hello names the computer without a token", (status, hello["name"], hello["api"]), (200, "Test Laptop", 2))
check("but says nothing else", set(hello), {"app", "id", "name", "api"})

check("state needs a paired device", jcall("/api/state")[0], 401)
check("asking needs one", call("/api/ask", {"text": "open youtube"})[0], 401)
check("and nothing got through", services.asked, [])

# pairing over HTTP, approved by the computer's own admin client
check("admin routes need the admin key", call("/api/admin/pair/start", {})[0], 403)
admin = {"X-Toby-Admin": store.admin_key}
check("and can't be reached through the Tailscale proxy, even with the key",
      call("/api/admin/pair/start", {}, headers={**admin, "Tailscale-User-Login": "me@example.com"})[0], 403)
status, session = jcall("/api/admin/pair/start", {"base_url": "https://toby.tail.ts.net"}, headers=admin)
check("the computer starts a session", (status, len(session["code"])), (200, 8))
status, bad = jcall("/api/pair/claim", {"code": "AAAAAAAA", "device_name": "x", "device_id": "device-x-12345"})
check("a wrong code is refused", status, 403)
status, claim = jcall("/api/pair/claim", {"code": session["code"], "device_name": "Jacob's iPhone",
                                          "device_id": "iphone-1234-5678", "platform": "ios"})
check("the right code is accepted", status, 200)
check("with the comparison number", claim["compare"], rb.compare_code(session["code"], "iphone-1234-5678"))
check("and the computer's identity", claim["computer"]["name"], "Test Laptop")
status, claims = jcall(f"/api/admin/pair/claims?session={session['id']}", headers=admin)
check("the computer sees who's asking", [c["name"] for c in claims["claims"]], ["Jacob's iPhone"])
check("and the same number", claims["claims"][0]["compare"], claim["compare"])

got = {}
waiter = threading.Thread(target=lambda: got.update(jcall(f"/api/pair/wait?claim={claim['claim']}")[1]))
waiter.start()
time.sleep(0.3)
check("a phone can't approve its own pairing", call("/api/admin/pair/decide", {"claim": claim["claim"], "allow": True})[0], 403)
jcall("/api/admin/pair/decide", {"claim": claim["claim"], "allow": True}, headers=admin)
waiter.join(5)
phone = got.get("token", "")
check("the waiting phone gets its token the moment you approve", len(phone) >= 40, True)
check("once", "token" in jcall(f"/api/pair/wait?claim={claim['claim']}")[1], False)
check("an unknown claim says so", jcall("/api/pair/wait?claim=nope")[1]["status"], "unknown")

check("the token works", call("/api/state", auth=phone)[0], 200)
check("a wrong token is refused", call("/api/state", auth="x" * len(phone))[0], 401)
check("a prefix of the right token is refused", call("/api/state", auth=phone[:-1])[0], 401)
check("the right token with junk after is refused", call("/api/state", auth=phone + "A")[0], 401)
status, state = jcall("/api/state", auth=phone)
check("state says which computer", state["computer"]["name"], "Test Laptop")
check("and which device is asking", state["device"]["name"], "Jacob's iPhone")

status, body = jcall("/api/ask", {"text": "open youtube"}, auth=phone)
check("a request is accepted", (status, body["ok"], body["task_id"]), (200, True, "task-1"))
check("and reaches Toby with who sent it", services.asked, [("open youtube", "Jacob's iPhone")])
check("empty text is refused", call("/api/ask", {"text": "   "}, auth=phone)[0], 400)
check("very long text is refused", call("/api/ask", {"text": "a" * 5000}, auth=phone)[0], 413)
check("an oversized body is refused", call("/api/ask", b"{" + b" " * (rb.MAX_BODY + 10) + b"}", auth=phone)[0], 400)
check("junk JSON is refused", call("/api/ask", b"not json", auth=phone)[0], 400)
check("a non-object body is refused", call("/api/ask", b"[1,2]", auth=phone)[0], 400)
services.busy = True
check("while busy, a second request says so", call("/api/ask", {"text": "x"}, auth=phone)[0], 409)
services.busy = False

check("an approval needs an id and a real true/false", call("/api/approve", {"id": "a1", "allow": "yes"}, auth=phone)[0], 400)
jcall("/api/approve", {"id": "a1", "allow": True}, auth=phone)
check("an approval reaches Toby with who answered", services.approved, [("a1", True, "Jacob's iPhone")])
bridge.publish({"approvals": [{"id": "a2", "title": "Run npm test"}]})
jcall("/api/confirm", {"answer": False}, auth=phone)
check("the web app's old confirm answers the oldest prompt", services.approved[-1], ("a2", False, "Jacob's iPhone"))
for action in ("pause", "resume", "stop"):
    jcall(f"/api/task/{action}", {}, auth=phone)
jcall("/api/cancel", {}, auth=phone)
check("task controls reach Toby", services.controls, ["pause", "resume", "stop", "stop"])

check("status comes from the computer", jcall("/api/status", auth=phone)[1]["hostname"], "test")
status, body = jcall("/api/screen", auth=phone)
check("the screen view says plainly when it's off", (status, body["code"]), (403, "screen_view_off"))
status, body = jcall("/api/overview", auth=phone)
check("something the computer can't do says so instead of pretending", (status, body["code"]), (501, "unavailable"))

# notifications
bridge.events.add("task_done", "Your build finished", "npm run build succeeded")
bridge.events.add("task_done", "Your build finished", "npm run build succeeded")
status, body = jcall("/api/events?after=0", auth=phone)
check("events reach the phone, duplicates dropped", [e["title"] for e in body["events"]], ["Your build finished"])
check("and can be fetched from a point", jcall(f"/api/events?after={body['last']}", auth=phone)[1]["events"], [])

# -- long polling --------------------------------------------------------------------------
v = jcall("/api/state", auth=phone)[1]["version"]


def later():
    time.sleep(0.4)
    bridge.publish({"busy": True, "steps": [{"label": "Open YouTube", "status": "current"}],
                    "reply": "", "task": {"id": "task-1", "state": "working"}, "approvals": []})


threading.Thread(target=later).start()
started = time.monotonic()
status, body, headers = call(f"/api/state?since={v}", auth=phone)
elapsed = time.monotonic() - started
state = json.loads(body)
check("the long poll returns as soon as something changes", 0.3 < elapsed < 3, True)
check("with the new state", state["steps"][0]["label"], "Open YouTube")
check("and a newer version", state["version"], v + 1)
check("API responses aren't cached", headers.get("Cache-Control"), "no-store")

# -- devices, revoking, logging out -------------------------------------------------------
second_device, second_token = store.add("Old iPad", "ios", "ipad-0000-1111")
status, body = jcall("/api/devices", auth=phone)
check("the phone can list paired devices", sorted(d["name"] for d in body["devices"]), ["Jacob's iPhone", "Old iPad"])
check("and knows which one it is", [d["name"] for d in body["devices"] if d["this_device"]], ["Jacob's iPhone"])
check("no token hashes are sent", any("token_sha256" in d for d in body["devices"]), False)
jcall("/api/devices/revoke", {"id": "ipad-0000-1111"}, auth=phone)
check("a lost device can be revoked from another", call("/api/state", auth=second_token)[0], 401)
jcall("/api/logout", {}, auth=phone)
check("logging out revokes this phone", call("/api/state", auth=phone)[0], 401)

# -- lockout ---------------------------------------------------------------------------------
_d, good = store.add("Phone", "ios", "phone-lock-0001")
for _ in range(rb.LOCKOUT_FAILURES):
    call("/api/state", auth="guess" * 10)
check("after repeated wrong tokens, even the right one is locked out for a while",
      call("/api/state", auth=good)[0], 429)
check("and so is pairing", call("/api/pair/claim", {"code": "AAAAAAAA", "device_id": "zzzzzzzzzz"})[0], 429)
bridge.stop()

# -- pairing from the terminal (`toby phone pair`) ----------------------------------------
import toby_cli  # noqa: E402

rb.PAIRING_PATH = workdir / "cli.json"
cli_store = rb.DeviceStore()
cli_bridge = rb.RemoteBridge(Services(), cli_store, port=0, name="CLI Laptop").start()
cli_port = cli_bridge.address[1]
said = []
toby_cli.say = said.append
toby_cli.show_qr = lambda text: False
answers = []
outcome = {}
cli = threading.Thread(target=lambda: outcome.setdefault(
    "rc", toby_cli.pair_phone({"remote_bind": "0.0.0.0"}, cli_port,
                              ask=lambda prompt: answers.append(prompt) or "y")))
cli.start()
deadline = time.time() + 5
typed = None
while time.time() < deadline and typed is None:
    for line in said:
        if "type the address and code by hand" in line:
            typed = line.split()[-1]
    time.sleep(0.05)
check("the terminal shows a code to type", bool(typed) and len(rb.normalize_code(typed)), 8)
base = f"http://127.0.0.1:{cli_port}"
status, claim = jcall("/api/pair/claim", {"code": typed, "device_name": "Terminal-paired phone",
                                          "device_id": "cli-phone-0001", "platform": "web"})
status, result = jcall(f"/api/pair/wait?claim={claim['claim']}")
cli.join(40)
check("the terminal asked before pairing", len(answers), 1)
check("and showed the same number as the phone",
      any(claim["compare"][:3] + " " + claim["compare"][3:] in line for line in said), True)
check("the phone got its token", result.get("status"), "approved")
check("and it works", call("/api/state", auth=result.get("token", ""))[0], 200)
check("the command reports success", outcome.get("rc"), 0)
cli_bridge.stop()

if failures:
    print(f"{len(failures)} PROBLEM(S):")
    for f in failures:
        print("  ", f)
    sys.exit(1)
print("phone bridge checks passed")
