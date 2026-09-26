"""The overnight queue inside the real app, with a scripted model.

A queued task that only looks runs and is ticked off with the real result.
One that wants to delete files doesn't wait ten minutes for an answer that
won't come, doesn't delete anything, and is left for you, saying what it
wanted. The morning tally goes to paired phones and Telegram."""
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _layer_shell_stub  # noqa: E402

_layer_shell_stub.install()
from gi.repository import GLib  # noqa: E402

import linux_agent_apple as app  # noqa: E402
import task_queue  # noqa: E402


def check(condition, what):
    if not condition:
        raise AssertionError(what)
    print("  ok:", what)


def pump(seconds, until=None):
    end = time.monotonic() + seconds
    ctx = GLib.MainContext.default()
    while time.monotonic() < end:
        ctx.iteration(False)
        if until and until():
            return True
        time.sleep(0.004)
    return False


home = Path(os.environ["HOME"])
(home / "project").mkdir(exist_ok=True)
(home / "project" / "notes.txt").write_text("Remember to water the tomatoes.\n")
(home / "Downloads").mkdir(exist_ok=True)
old_zip = home / "Downloads" / "old.zip"
old_zip.write_text("zip")

app.SETTINGS.update(telegram_enabled=True, remote_port=0, notes_folder="")
win = app.AssistantWindow(app.RingFlash())
now = {"t": datetime(2026, 9, 27, 1, 30)}
held = []
win.queue.clock = lambda: now["t"]
win.queue.state_path = home / "queue_state.json"
win.queue.inhibit = lambda: held.append("held") or (lambda: held.append("released"))

qfile = app.queue_file()
check(qfile == home / "linux-agent" / "Queue.md", "without a notes folder, the queue lives in ~/linux-agent")
task_queue.add(qfile, "Read ~/project/notes.txt and tell me what it says")
task_queue.add(qfile, "Tidy my Downloads")

rounds = []


def scripted_think(instruction, history, on_chunk=None, cancel_check=None):
    rounds.append(instruction)
    if "WHAT YOUR ACTIONS RETURNED" in instruction:
        if "water the tomatoes" in instruction:
            return {"actions": [], "reply": "It says to water the tomatoes.", "mood": "happy"}
        return {"actions": [], "reply": "I couldn't delete anything without your OK.", "mood": "concerned"}
    if instruction.startswith("Read ~/project/notes.txt"):
        return {"actions": [{"tool": "read_file", "path": "~/project/notes.txt"}], "reply": "Reading.", "mood": "neutral"}
    if instruction.startswith("Tidy my Downloads"):
        return {"actions": [{"tool": "trash_files", "paths": ["~/Downloads/old.zip"]}],
                "reply": "Tidying.", "mood": "neutral"}
    return {"actions": [], "reply": "I couldn't delete anything without your OK.", "mood": "concerned"}


app.think = scripted_think

started = time.monotonic()
win._queue_tick()
pump(20, until=lambda: not win.queue.running)
items = task_queue.parse(qfile.read_text())
check(items[0].state == "x", "the first task ran and is ticked")
record = next(t for t in app.TASKS.recent(5) if t["text"].startswith("Read ~/project/notes.txt"))
check(record["origin"] == "queue" and record["reply"] == "It says to water the tomatoes.",
      "through the normal task path, with the real file's contents")
out = qfile.parent / "Outbox"
check(any("water the tomatoes" in f.read_text() for f in out.glob("*.md")), "its result is in the outbox")

win._queue_tick()
pump(20, until=lambda: not win.queue.running)
took = time.monotonic() - started
items = task_queue.parse(qfile.read_text())
check(items[1].state == "!" and "needs you" in qfile.read_text().splitlines()[-1], "the delete is left for you")
check(old_zip.exists(), "and nothing was deleted")
check(not win.approvals.pending(), "no question was left waiting (nobody's there to answer it)")
check(took < 30, f"it didn't sit waiting for an answer ({took:.0f}s for both)")
tidy = app.TASKS.get(next(t for t in app.TASKS.recent(5) if t["text"] == "Tidy my Downloads")["id"])
step = next(st for st in tidy["steps"] if st["label"].startswith("Delete"))
check(step["status"] == "error" and step["detail"].startswith("Left for you"), "the step says it was left for you")
check(held == ["held", "released"] * 2, "idle sleep was held off while each ran")

now["t"] = datetime(2026, 9, 27, 7, 2)
win._queue_tick()
pump(1)
events = [e for e in win.remote.events.since(0) if e["kind"] == "queue"]
check(len(events) == 1 and events[0]["title"] == "From your queue: 1 done, 1 needs you",
      "in the morning, one tally goes to paired phones and Telegram")

said = app.DISPATCH["queue_task"]({"text": "Summarize ~/project/notes.txt", "at": ""})
check("Added to the queue" in said and "tonight" in said and
      task_queue.parse(qfile.read_text())[-1].text == "Summarize ~/project/notes.txt",
      "\"do this tonight\" adds to the queue")
check("14:30" in app.DISPATCH["queue_task"]({"text": "check the build", "at": "14:30"}), "or at a time")
win.remote.stop()
print("queue-in-the-app checks passed")
