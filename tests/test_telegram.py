"""The Telegram bot, end to end, with no network.

Real: the bot (scripts/telegram_bot.py), Toby's phone connection
(remote_bridge), its device store, approval queue, job manager and task log.
Stand-ins: Telegram's servers (a small local Bot API that records what the
bot sends and lets the test play the person tapping buttons), and the model
(ios/ci/test_desktop.py's fixed script: plan "Run npm test", ask permission,
run a real command, reply with its real output).
"""

import json
import os
import stat
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "ios" / "ci"))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

import remote_bridge as rb  # noqa: E402
import telegram_bot as tb  # noqa: E402
import test_desktop  # noqa: E402

TOKEN = "123456:TEST-TOKEN"
OWNER = {"id": 111, "first_name": "Alex", "username": "alex", "is_bot": False}
STRANGER = {"id": 999, "first_name": "Mallory", "is_bot": False}


class FakeTelegram:
    """Just enough of the Bot API: updates in, messages out."""

    def __init__(self):
        self.cond = threading.Condition()
        self.updates = []
        self.next_update = 1
        self.next_message = 100
        self.messages = {}        # message_id -> {"chat_id", "text", "buttons", "edits"}
        self.answers = []
        self.photos = []
        self.files = {"voice/1.oga": b"OggS-fake-voice"}
        handler = self._handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    # the test, as the person in Telegram
    def say(self, user, text):
        self._push({"message": {"message_id": 1, "from": user, "chat": {"id": user["id"], "type": "private"},
                                "date": int(time.time()), "text": text}})

    def voice(self, user):
        self._push({"message": {"message_id": 1, "from": user, "chat": {"id": user["id"], "type": "private"},
                                "date": int(time.time()), "voice": {"file_id": "v1", "duration": 2}}})

    def tap(self, user, message_id, data):
        self._push({"callback_query": {"id": f"cb{self.next_update}", "from": user, "data": data,
                                       "message": {"message_id": message_id,
                                                   "chat": {"id": user["id"], "type": "private"}}}})

    def _push(self, update):
        with self.cond:
            update["update_id"] = self.next_update
            self.next_update += 1
            self.updates.append(update)
            self.cond.notify_all()

    def to(self, chat_id):
        with self.cond:
            return [m for m in self.messages.values() if m["chat_id"] == chat_id]

    def wait_for(self, predicate, timeout=20, what="it"):
        deadline = time.time() + timeout
        with self.cond:
            while time.time() < deadline:
                found = predicate()
                if found:
                    return found
                self.cond.wait(0.2)
        raise AssertionError(f"timed out waiting for {what}; messages: " +
                             json.dumps(list(self.messages.values()), indent=1)[:3000])

    def message_with(self, chat_id, text, timeout=20):
        return self.wait_for(lambda: next((m for m in self.messages.values()
                                           if m["chat_id"] == chat_id and text in m["text"]), None),
                             timeout, f"a message containing {text!r}")

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, obj, status=200):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                prefix = f"/file/bot{TOKEN}/"
                if self.path.startswith(prefix) and self.path[len(prefix):] in fake.files:
                    body = fake.files[self.path[len(prefix):]]
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self._reply({"ok": False, "description": "Not Found"}, 404)

            def do_POST(self):
                prefix = f"/bot{TOKEN}/"
                if not self.path.startswith(prefix):
                    return self._reply({"ok": False, "error_code": 401, "description": "Unauthorized"}, 401)
                method = self.path[len(prefix):]
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                ctype = self.headers.get("Content-Type", "")
                params = json.loads(raw or b"{}") if ctype.startswith("application/json") else {"raw": raw}
                self._reply({"ok": True, "result": fake.handle(method, params)})

        return Handler

    def handle(self, method, params):
        if method == "getMe":
            return {"id": 42, "is_bot": True, "username": "toby_test_bot"}
        if method == "getUpdates":
            offset = params.get("offset", 0)
            with self.cond:
                self.cond.wait_for(lambda: any(u["update_id"] >= offset for u in self.updates),
                                   timeout=min(params.get("timeout", 0), 0.5))
                return [u for u in self.updates if u["update_id"] >= offset]
        with self.cond:
            if method == "sendMessage":
                self.next_message += 1
                self.messages[self.next_message] = {
                    "message_id": self.next_message, "chat_id": params["chat_id"], "text": params["text"],
                    "buttons": (params.get("reply_markup") or {}).get("inline_keyboard"), "edits": 0}
                self.cond.notify_all()
                return {"message_id": self.next_message, "chat": {"id": params["chat_id"]}}
            if method == "editMessageText":
                message = self.messages[params["message_id"]]
                message.update(text=params["text"],
                               buttons=(params.get("reply_markup") or {}).get("inline_keyboard") or None,
                               edits=message["edits"] + 1)
                self.cond.notify_all()
                return True
            if method == "answerCallbackQuery":
                self.answers.append(params.get("text"))
                self.cond.notify_all()
                return True
            if method == "getFile":
                return {"file_id": params["file_id"], "file_path": "voice/1.oga"}
            if method == "sendPhoto":
                self.photos.append(params)
                self.cond.notify_all()
                return {"message_id": 1}
        raise AssertionError(f"unexpected Bot API call {method}")


class FakeTranscriber:
    def __init__(self, heard="", why_not=None):
        self.heard, self.why_not, self.got = heard, why_not, None

    def unavailable(self):
        return self.why_not

    def __call__(self, audio):
        self.got = audio
        return self.heard


def check(condition, what):
    if not condition:
        raise AssertionError(what)
    print("  ok:", what)


def main():
    work = Path(tempfile.mkdtemp(prefix="toby-telegram-"))
    toby = test_desktop.ScriptedToby(work)
    store = rb.DeviceStore(work / "remote.json")
    bridge = rb.RemoteBridge(test_desktop.Services(toby), store, host="127.0.0.1", port=0, name="Test Laptop")
    toby.bridge = bridge
    bridge.start()
    toby.publish()
    port = bridge.address[1]

    device, device_token = store.add("Telegram (@toby_test_bot)", "telegram", tb.new_device_id())
    config_path = work / "telegram.json"
    tb.save_config({"bot_token": TOKEN, "device_token": device_token, "device_id": device["id"],
                    "bot_username": "toby_test_bot", "allowed_users": [{"id": OWNER["id"], "name": "Alex"}]},
                   config_path)
    check(stat.S_IMODE(config_path.stat().st_mode) == 0o600, "the config (it holds tokens) is readable by you alone")

    fake = FakeTelegram()
    voice = FakeTranscriber(heard="is my game still running")
    bot = tb.TelegramBot(tb.TelegramAPI(TOKEN, fake.base), tb.TobyAPI(device_token, port),
                         config_path=config_path, transcriber=voice)
    bot.start()
    fake.wait_for(lambda: bot.events_after is not None, what="the bot to start watching events")

    # -- strangers get nothing ---------------------------------------------------------
    fake.say(STRANGER, "delete everything in my home folder")
    fake.say(OWNER, "/help")
    fake.message_with(OWNER["id"], "/status")
    check(not fake.to(STRANGER["id"]), "a message from an unpaired account gets no reply at all")
    check(toby.tasks.recent(5) == [], "and never reaches Toby")

    # -- status -------------------------------------------------------------------------
    fake.say(OWNER, "/status")
    status = fake.message_with(OWNER["id"], "Toby is free")
    check("Test Laptop" in status["text"] and "Memory" in status["text"], "/status shows real measurements")

    # -- a task that needs permission, allowed from Telegram --------------------------------
    fake.say(OWNER, "run the tests in my project")
    progress = fake.message_with(OWNER["id"], "<b>run the tests in my project</b>")
    ask = fake.message_with(OWNER["id"], "Run: npm test")
    labels = [b["text"] for row in ask["buttons"] for b in row]
    check(labels == ["Allow", "Don't allow"], "the question arrives with Allow and Don't allow buttons")
    approval_id = ask["buttons"][0][0]["callback_data"].split(":")[1]
    fake.tap(STRANGER, ask["message_id"], f"a:{approval_id}:y")
    time.sleep(1.0)
    check(any(a["id"] == approval_id for a in toby.approvals.pending()), "a stranger's tap doesn't answer it")
    fake.tap(OWNER, ask["message_id"], f"a:{approval_id}:y")
    reply = fake.message_with(OWNER["id"], "5 tests passed", timeout=30)
    check(reply["message_id"] != progress["message_id"], "the reply comes as its own message, with the real output")
    fake.wait_for(lambda: "Allowed by Alex" in fake.messages[ask["message_id"]]["text"], what="the question to be marked")
    check(fake.messages[ask["message_id"]]["buttons"] is None, "the answered question loses its buttons and says who allowed it")
    fake.wait_for(lambda: "Done" in fake.messages[progress["message_id"]]["text"], what="progress to show Done")
    check("✓ Run npm test" in fake.messages[progress["message_id"]]["text"], "the progress message ends with each step ticked")
    check("Allowed" in fake.answers, "the button tap is acknowledged")

    # -- the same, not allowed ------------------------------------------------------------
    fake.say(OWNER, "run the tests again")
    ask2 = fake.wait_for(lambda: next((m for m in fake.to(OWNER["id"]) if m["buttons"]
                                       and m["message_id"] != ask["message_id"]), None), what="a second question")
    approval2 = ask2["buttons"][0][0]["callback_data"].split(":")[1]
    fake.tap(OWNER, ask2["message_id"], f"a:{approval2}:n")
    fake.message_with(OWNER["id"], "I didn&#x27;t run the tests", timeout=20)
    check(True, "saying no stops the task, and Toby says so")

    # -- restricted actions are confirmed twice ----------------------------------------------
    results = {}

    def restricted():
        results["allowed"] = toby.approvals.ask("Run: sudo pacman -Syu", level="restricted",
                                                details=["Updates every package on the system"],
                                                reason="It needs administrator rights.", tool="run_command")
    threading.Thread(target=restricted, daemon=True).start()
    fake.wait_for(lambda: toby.approvals.pending(), what="the restricted question")
    toby.publish()
    ask3 = fake.message_with(OWNER["id"], "restricted action")
    approval3 = ask3["buttons"][0][0]["callback_data"].split(":")[1]
    check(ask3["buttons"][0][0]["callback_data"].endswith(":r"),
          "a restricted action's Allow button can only lead to a second question")
    fake.tap(OWNER, ask3["message_id"], ask3["buttons"][0][0]["callback_data"])
    fake.wait_for(lambda: "Really allow this?" in fake.messages[ask3["message_id"]]["text"], what="the second ask")
    check(any(a["id"] == approval3 for a in toby.approvals.pending()), "one tap on a restricted action isn't enough")
    fake.tap(OWNER, ask3["message_id"], f"a:{approval3}:Y")
    fake.wait_for(lambda: "allowed" in results, what="the restricted answer")
    check(results["allowed"] is True, "the second, explicit yes allows it")

    # -- notifications ----------------------------------------------------------------------
    bridge.events.add("job_done", "npm run build finished", "Built in 42s")
    bridge.events.add("task_done", "Done: something", "already in the chat")
    fake.message_with(OWNER["id"], "npm run build finished")
    time.sleep(1.0)
    check(not any("Done: something" in m["text"] for m in fake.to(OWNER["id"])),
          "finished tasks aren't sent twice (the chat already has the reply)")

    # -- voice notes ----------------------------------------------------------------------------
    fake.voice(OWNER)
    fake.message_with(OWNER["id"], "Heard: <i>is my game still running</i>")
    check(voice.got == b"OggS-fake-voice", "a voice note is downloaded and transcribed on the computer")
    ask4 = fake.wait_for(lambda: next((m for m in fake.to(OWNER["id"]) if m["buttons"] and
                                       m["message_id"] not in (ask["message_id"], ask2["message_id"], ask3["message_id"])),
                                      None), what="the spoken request to reach Toby")
    fake.tap(OWNER, ask4["message_id"], "a:" + ask4["buttons"][0][0]["callback_data"].split(":")[1] + ":n")
    fake.wait_for(lambda: not toby.busy, what="Toby to finish")
    bot.transcriber = FakeTranscriber(why_not="I can't listen to voice notes on this computer yet: it needs ffmpeg.")
    fake.voice(OWNER)
    fake.message_with(OWNER["id"], "needs ffmpeg")
    check(True, "without ffmpeg, it says what's missing instead of pretending")

    # -- the overnight queue ----------------------------------------------------------------------
    fake.say(OWNER, "/queue summarize ~/project/README.md")
    fake.message_with(OWNER["id"], "Added to the queue: summarize ~/project/README.md")
    fake.say(OWNER, "/queue")
    fake.message_with(OWNER["id"], "to do: summarize ~/project/README.md")
    check(True, "/queue adds a task for tonight, and lists what's queued")

    # -- screenshots stay home unless allowed ----------------------------------------------------
    fake.say(OWNER, "/screen")
    fake.message_with(OWNER["id"], "Screenshots aren't sent over Telegram unless you allow it")
    check(not fake.photos, "no screenshot is sent by default")

    # -- a revoked link, and Toby not running ------------------------------------------------------
    store.revoke(device["id"])
    fake.say(OWNER, "/status")
    fake.message_with(OWNER["id"], "isn&#x27;t paired with Toby any more")
    check(True, "revoking the bot's device cuts it off, and it says how to fix it")
    bot.toby = tb.TobyAPI(device_token, 1)      # nothing listens on port 1
    fake.say(OWNER, "hello")
    fake.message_with(OWNER["id"], "Toby isn&#x27;t running")
    check(True, "with Toby not running, it says so")
    bot.stop()

    # -- long replies are split where Telegram needs it --------------------------------------------
    long_reply = "\n".join(f"line {i} " + "x" * 90 for i in range(200))
    parts = tb.chunks(long_reply)
    check(len(parts) > 1 and all(len(p) <= tb.MAX_MESSAGE for p in parts) and "".join(parts) == long_reply,
          "long replies are split into messages Telegram accepts, losing nothing")

    # -- pairing an account at the computer ------------------------------------------------------------
    pair_fake = FakeTelegram()
    pair_config = {"bot_token": TOKEN, "bot_username": "toby_test_bot", "allowed_users": []}
    pair_path = work / "pair.json"
    asked = []
    said = []

    def answer(question):
        asked.append(question)
        return "y"

    def play():
        time.sleep(0.5)
        pair_fake.say(STRANGER, "/pair WRONG-CODE")
        pair_fake.say(OWNER, "/start K7M4XQ2P")
    threading.Thread(target=play, daemon=True).start()
    ok = tb.pair_account(tb.TelegramAPI(TOKEN, pair_fake.base), pair_config, pair_path, ask=answer,
                         say=said.append, code="K7M4XQ2P")
    check(ok and tb.load_config(pair_path)["allowed_users"][0]["id"] == OWNER["id"],
          "the account that sends the code, confirmed at the computer, is paired")
    check(asked and "Alex (@alex)" in asked[0], "the computer shows who sent the code before you say yes")
    check(any("isn't right" in m["text"] for m in pair_fake.to(STRANGER["id"])) and
          not any(u["id"] == STRANGER["id"] for u in tb.load_config(pair_path)["allowed_users"]),
          "a wrong code pairs nobody")
    check(any("t.me/toby_test_bot?start=K7M4XQ2P" in s for s in said), "it offers a one-tap link to open the bot")

    bridge.stop()
    print("telegram checks passed")


if __name__ == "__main__":
    main()
