"""Drive the phone app in a real browser (headless Chromium via Playwright).

Serves mobile/ from a real RemoteBridge, opens it at iPhone size with the
pairing link, and checks what a person would see: the token is taken from
the link and wiped from the address bar, the live checklist strikes through
finished steps, the permission prompt appears and its buttons reach the
laptop, and a sent message arrives. Screenshots are saved for looking at.

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


class CB:
    def __init__(self):
        self.asked, self.confirmed = [], []

    def ask(self, text):
        self.asked.append(text)
        return True, "Sent."

    def confirm(self, answer):
        self.confirmed.append(answer)
        return True

    def cancel(self):
        return True


cb = CB()
token = rb.reset_token(Path(tempfile.mkdtemp()) / "remote.json")
bridge = rb.RemoteBridge(cb, token, port=0).start()
host, port = bridge.address
url = f"http://{host}:{port}/"

busy_state = {
    "busy": True, "task": "Search Khan Academy for quadratics", "reply": "",
    "steps": [{"label": "Open www.khanacademy.org", "status": "done"},
              {"label": "Move the pointer", "status": "done"},
              {"label": "Click", "status": "current"},
              {"label": 'Type "quadratics"', "status": "pending"}],
    "confirm": None, "history": []}
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
    page.goto(url + "#pair=" + token)
    page.wait_for_selector("#steps li")
    time.sleep(0.6)   # let the entrance animation finish before the screenshot

    if "pair=" in page.url:
        failures.append("the pairing token was left in the address bar")
    if page.evaluate("localStorage.getItem('toby.pairing')") != token:
        failures.append("the pairing token wasn't kept")
    classes = page.eval_on_selector_all("#steps li", "els => els.map(e => e.className)")
    if classes != ["done", "done", "current", "pending"]:
        failures.append(f"step states rendered wrong: {classes}")
    strike = page.eval_on_selector("#steps li.done", "e => getComputedStyle(e).textDecorationLine")
    if "line-through" not in strike:
        failures.append("finished steps aren't struck through")
    pending_strike = page.eval_on_selector("#steps li.pending", "e => getComputedStyle(e).textDecorationLine")
    if "line-through" in pending_strike:
        failures.append("steps not yet done are struck through")
    page.screenshot(path=str(out_dir / "phone_task.png"))

    # a permission prompt arrives, and the phone answers it
    bridge.publish({**busy_state, "confirm": {"pending": True, "text": "Let Toby use your mouse and keyboard?"}})
    page.wait_for_selector("#confirm:not([hidden])", timeout=25000)
    time.sleep(0.4)
    page.screenshot(path=str(out_dir / "phone_confirm.png"))
    page.click("#confirm-yes")
    time.sleep(0.4)
    if cb.confirmed != [True]:
        failures.append(f"tapping Yes didn't reach the laptop: {cb.confirmed}")

    # the reply arrives
    bridge.publish({"busy": False, "task": "", "steps": busy_state["steps"][:2], "confirm": None,
                    "reply": "Done. Khan Academy is searching for quadratics.", "history": []})
    # (polled from here rather than with wait_for_function: the app's
    # Content Security Policy rightly refuses string-evaluated script)
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline and not (page.text_content("#reply") or "").startswith("Done"):
        time.sleep(0.2)
    if not (page.text_content("#reply") or "").startswith("Done"):
        failures.append("the reply never showed on the phone")
    time.sleep(0.6)
    page.screenshot(path=str(out_dir / "phone_reply.png"))
    if page.text_content("#task-title") != "Done":
        failures.append(f"a finished task isn't marked done: {page.text_content('#task-title')!r}")
    if page.is_visible("#cancel"):
        failures.append("Cancel is still offered for a finished task")

    # sending a message
    page.fill("#text", "plan out my week")
    page.click("#send")
    time.sleep(0.5)
    if cb.asked != ["plan out my week"]:
        failures.append(f"sending didn't reach the laptop: {cb.asked}")

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
