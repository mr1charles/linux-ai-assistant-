"""Drive the phone app in a real browser (headless Chromium via Playwright).

Serves mobile/ from a real RemoteBridge, opens it at phone size with a
pairing link, and checks what a person would see: the code is wiped from the
address bar, the phone shows the same comparison number as the computer and
gets a working token once the computer approves; the live checklist strikes
through finished steps; permission prompts say what and why, and their
buttons reach the computer; a revoked phone goes back to pairing.
Screenshots are saved for looking at.

Needs the playwright Python package and a Chromium; skipped otherwise.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("skipped: playwright not installed")
    sys.exit(0)

import remote_bridge as rb  # noqa: E402

failures = []
out_dir = Path(os.environ.get("PHONE_SHOTS", tempfile.mkdtemp()))


class Services:
    def __init__(self):
        self.asked, self.approved, self.controls = [], [], []

    def ask(self, text, device):
        self.asked.append(text)
        return True, "Sent.", "t1"

    def approve(self, approval_id, allow, device):
        self.approved.append((approval_id, allow))
        return True

    def task_control(self, action, device):
        self.controls.append(action)
        return True, "ok"


cb = Services()
store = rb.DeviceStore(Path(tempfile.mkdtemp()) / "remote.json")
bridge = rb.RemoteBridge(cb, store, port=0, name="Jacob's Laptop").start()
host, port = bridge.address
url = f"http://{host}:{port}/"
session = bridge.pairing.start(url)

steps = [{"label": "Open www.khanacademy.org", "status": "done", "detail": "Opened https://www.khanacademy.org"},
         {"label": "Move the pointer", "status": "done", "detail": ""},
         {"label": "Click", "status": "current", "detail": ""},
         {"label": 'Type "quadratics"', "status": "pending", "detail": ""}]
busy_state = {
    "busy": True, "reply": "", "steps": steps, "approvals": [], "history": [],
    "task": {"id": "t1", "text": "Search Khan Academy for quadratics", "state": "working", "steps": steps}}
bridge.publish(busy_state)

executable = None
for candidate in sorted(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome")):
    executable = str(candidate)

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=executable,
                                args=["--no-proxy-server", "--no-sandbox"])
    page = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                            is_mobile=True, has_touch=True)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url + "#pair=" + session["code"])
    page.wait_for_selector("#pair-status .compare", timeout=15000)
    if "pair=" in page.url:
        failures.append("the pairing code was left in the address bar")
    claims = bridge.pairing.pending_claims()
    shown = page.text_content("#pair-status .compare").replace(" ", "")
    if not claims or claims[0]["compare"] != shown:
        failures.append(f"the phone shows a different number from the computer: {shown} vs {claims}")
    page.screenshot(path=str(out_dir / "phone_pairing.png"))
    if claims:
        bridge.pairing.decide(claims[0]["id"], True)
    page.wait_for_selector("#steps li", timeout=25000)
    time.sleep(0.6)   # let the entrance animation finish before the screenshot
    saved = page.evaluate("localStorage.getItem('toby.pairing')")
    if not saved or not store.authenticate(saved):
        failures.append("the phone didn't keep a working token after approval")
    if page.text_content("#task-state") != "Working":
        failures.append(f"the task's state isn't shown: {page.text_content('#task-state')!r}")
    classes = page.eval_on_selector_all("#steps li", "els => els.map(e => e.className)")
    if classes != ["done", "done", "current", "pending"]:
        failures.append(f"step states rendered wrong: {classes}")
    strike = page.eval_on_selector("#steps li.done .label", "e => getComputedStyle(e).textDecorationLine")
    if "line-through" not in strike:
        failures.append("finished steps aren't struck through")
    pending_strike = page.eval_on_selector("#steps li.pending .label", "e => getComputedStyle(e).textDecorationLine")
    if "line-through" in pending_strike:
        failures.append("steps not yet done are struck through")
    detail_strike = page.eval_on_selector("#steps li.done .detail", "e => getComputedStyle(e).textDecorationLine")
    if "line-through" in detail_strike:
        failures.append("a finished step's detail is struck through too")
    page.screenshot(path=str(out_dir / "phone_task.png"))

    # a permission prompt arrives, and the phone answers it
    approval = {"id": "a1", "kind": "action", "level": "confirm",
                "title": "Delete 14 files from ~/Downloads/Old Projects",
                "details": ["~/Downloads/Old Projects/f1.zip", "They go to the trash, so you can restore them."],
                "reason": "deleting files", "created": 0, "expires": 0}
    bridge.publish({**busy_state, "approvals": [approval],
                    "task": {**busy_state["task"], "state": "needs_permission"}})
    page.wait_for_selector("#confirm:not([hidden])", timeout=25000)
    time.sleep(0.4)
    if "Delete 14 files" not in page.text_content("#confirm-text"):
        failures.append("the prompt doesn't say what Toby wants to do")
    if page.eval_on_selector_all("#confirm-details li", "els => els.length") != 2:
        failures.append("the prompt doesn't list the details")
    if page.text_content("#task-state") != "Needs your OK":
        failures.append("the task doesn't show that it needs permission")
    page.screenshot(path=str(out_dir / "phone_confirm.png"))
    page.click("#confirm-yes")
    time.sleep(0.4)
    if cb.approved != [("a1", True)]:
        failures.append(f"tapping Allow didn't reach the computer: {cb.approved}")
    page.click("#pause")
    time.sleep(0.3)
    if cb.controls != ["pause"]:
        failures.append(f"Pause didn't reach the computer: {cb.controls}")

    # a restricted request is flagged as such
    bridge.publish({**busy_state, "approvals": [{**approval, "id": "a2", "level": "restricted",
                                                 "title": "Run: sudo pacman -Syu", "reason": "it runs as administrator (sudo)"}]})
    page.wait_for_selector("#confirm.restricted", timeout=25000)
    if "sudo" not in (page.text_content("#confirm-reason") or ""):
        failures.append("a restricted request doesn't say why it's risky")

    # the reply arrives
    done_steps = [{**st, "status": "done"} for st in steps]
    bridge.publish({"busy": False, "steps": done_steps, "approvals": [], "history": [],
                    "reply": "Done. Khan Academy is searching for quadratics.",
                    "task": {**busy_state["task"], "state": "completed", "steps": done_steps}})
    # (polled from here rather than with wait_for_function: the app's
    # Content Security Policy rightly refuses string-evaluated script)
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline and not (page.text_content("#reply") or "").startswith("Done"):
        time.sleep(0.2)
    if not (page.text_content("#reply") or "").startswith("Done"):
        failures.append("the reply never showed on the phone")
    time.sleep(0.6)
    page.screenshot(path=str(out_dir / "phone_reply.png"))
    if page.text_content("#task-state") != "Done":
        failures.append(f"a finished task isn't marked done: {page.text_content('#task-state')!r}")
    if page.is_visible("#cancel"):
        failures.append("Stop is still offered for a finished task")

    # sending a message
    page.fill("#text", "plan out my week")
    page.click("#send")
    time.sleep(0.5)
    if cb.asked != ["plan out my week"]:
        failures.append(f"sending didn't reach the computer: {cb.asked}")

    # revoked on the computer, the phone goes back to the pairing card
    store.reset()
    page.fill("#text", "hello?")
    page.click("#send")
    page.wait_for_selector("#pair:not([hidden])", timeout=10000)
    if page.evaluate("localStorage.getItem('toby.pairing')"):
        failures.append("a revoked phone kept its token")

    # an unpaired phone sees the pairing card, not the app
    fresh = browser.new_page(viewport={"width": 390, "height": 844})
    fresh.goto(url)
    fresh.wait_for_selector("#pair:not([hidden])")
    if fresh.is_visible("#composer"):
        failures.append("an unpaired phone still shows the message bar")
    fresh.screenshot(path=str(out_dir / "phone_unpaired.png"))

    if errors:
        failures.append("the page threw: " + "; ".join(errors))
    browser.close()

bridge.stop()
print(f"screenshots in {out_dir}")
if failures:
    print(f"{len(failures)} PROBLEM(S):")
    for f in failures:
        print("  ", f)
    sys.exit(1)
print("phone app browser checks passed")
