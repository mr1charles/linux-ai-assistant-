"""The overnight queue's logic (scripts/task_queue.py): reading the file,
deciding what's due, ticking tasks off, the outbox, and the morning tally."""

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import task_queue as q  # noqa: E402


def check(condition, what):
    if not condition:
        raise AssertionError(what)
    print("  ok:", what)


work = Path(tempfile.mkdtemp(prefix="toby-queue-"))
path = work / "Toby" / "Queue.md"
q.ensure(path)
check(path.read_text().startswith("# Toby's queue"), "a new queue file explains itself")
q.add(path, "Find the five biggest folders in ~/Downloads")
q.add(path, "check whether ~/project builds", at="14:30")
with path.open("a") as f:
    f.write("- [ ] Read ~/project/README.md and write a summary\n  Keep it short.\n  Bullet points.\n"
            "- [x] An old one (done 02:10, see Outbox/2026-09-01 An old one.md)\n"
            "Some prose in between.\n")
items = q.parse(path.read_text())
check([i.state for i in items] == [" ", " ", " ", "x"], "tasks and their states are read")
check(items[1].at == (14, 30) and items[1].title == "check whether ~/project builds", "at 14:30: is read as a time")
check(items[2].prompt == "Read ~/project/README.md and write a summary Keep it short. Bullet points.",
      "indented lines are part of the task")
check(items[3].text == "An old one", "Toby's note after a ticked task isn't part of the task")

# -- what's due when ---------------------------------------------------------------------
state = {}
evening = datetime(2026, 9, 26, 22, 0)
check(q.next_due(items, evening, state) is None, "at 22:00 nothing is due (the night window starts at 01:00)")
night = datetime(2026, 9, 27, 1, 30)
check(q.next_due(items, night, state).text.startswith("Find the five"), "at 01:30 the first task is due")
late = [i for i in q.parse("- [ ] at 03:00: back up the project\n")]
seen = {}
q.next_due(late, datetime(2026, 9, 26, 22, 0), seen)
check(q.next_due(late, datetime(2026, 9, 26, 22, 5), seen) is None,
      "\"at 03:00\" written at 22:00 means tonight, not already late")
check(q.next_due(late, datetime(2026, 9, 27, 3, 1), seen) is not None, "and it's due at 03:00")
check(q.next_due(items[:1], datetime(2026, 9, 27, 15, 0), {}, run_now=True) is not None, "run now ignores the window")
check(q.in_window(datetime(2026, 9, 27, 23, 30), (22, 0), (6, 0)), "a window can wrap past midnight")

# -- ticking off survives edits -------------------------------------------------------------
first = items[0]
content = path.read_text()
path.write_text(content.replace("# Toby's queue", "# Toby's queue\n\nA line you added meanwhile."))
check(q.mark(path, first, "x", "done 01:40, see Outbox/x.md"), "a task is found by its words, even after edits")
check("- [x] Find the five biggest folders in ~/Downloads (done 01:40, see Outbox/x.md)" in path.read_text(),
      "and ticked with a note")
check(q.parse(path.read_text())[0].text == first.text, "re-reading gives the same task back")
check(not q.mark(path, q.Item(99, " ", "not there", None, []), "x", ""), "a task you deleted isn't recreated")

# -- the runner -------------------------------------------------------------------------------------
now = {"t": datetime(2026, 9, 27, 1, 30)}
runs, notices, inhibits = [], [], []
queue_file = work / "q2" / "Queue.md"
q.ensure(queue_file)
for text in ("Summarize my notes on the garden", "Tidy my Downloads", "Something that breaks"):
    q.add(queue_file, text)


def ask(prompt):
    runs.append(prompt)
    if prompt.startswith("Summarize"):
        return {"state": "completed", "reply": "Tomatoes need watering every morning.",
                "steps": [{"label": "Search your notes for \"garden\"", "status": "done", "detail": "3 passages"}]}
    if prompt.startswith("Tidy"):
        return {"state": "completed", "reply": "I'd delete 3 files, but that needs your OK.",
                "left_for_you": ["Delete 3 files from ~/Downloads"]}
    raise RuntimeError("the model stopped answering")


busy = {"v": False}
settings = {"queue_start": "01:00", "queue_end": "07:00"}


def inhibit():
    inhibits.append("held")
    return lambda: inhibits.append("released")


runner = q.QueueRunner(lambda: queue_file, ask, lambda: busy["v"], lambda: settings,
                       lambda title, body: notices.append((title, body)), inhibit=inhibit,
                       clock=lambda: now["t"], state_path=work / "state.json")
busy["v"] = True
check(runner.tick() is None, "nothing starts while Toby is busy with you")
busy["v"] = False
for expected in ("done", "needs you", "failed"):
    item = runner.tick()
    check(item is not None and runner.tick() is None, f"one task at a time ({item.title if item else None})")
    outcome = runner.run(item)
    check(outcome == expected, f"\"{item.title}\" ends as {expected}")
    now["t"] += timedelta(minutes=10)
check(runner.tick() is None, "and then there's nothing left")
text = queue_file.read_text()
check("- [x] Summarize my notes on the garden (done 01:30, see Outbox/2026-09-27 Summarize my notes on the garden.md)"
      in text, "a finished task is ticked with where its result is")
check("- [!] Tidy my Downloads (needs you: Delete 3 files from ~/Downloads; see" in text,
      "one that needed your OK says what it wanted")
check("- [!] Something that breaks (couldn't finish, 01:50; see" in text and "[~]" not in text,
      "one that failed says so, and nothing is left marked as working")
out = queue_file.parent / "Outbox" / "2026-09-27 Summarize my notes on the garden.md"
check(out.exists() and "Tomatoes need watering" in out.read_text() and "## Steps" in out.read_text(),
      "what Toby found is in the outbox, with its steps")
check("## Left for you" in (queue_file.parent / "Outbox" / "2026-09-27 Tidy my Downloads.md").read_text(),
      "and the outbox says what was left for you")
check(inhibits == ["held", "released"] * 3, "idle sleep is held off while each task runs, and let go after")

check(notices == [], "no notification in the night")
now["t"] = datetime(2026, 9, 27, 7, 1)
runner.tick()
check(len(notices) == 1 and notices[0][0] == "From your queue: 1 done, 1 needs you, 1 couldn't be finished",
      "one tally in the morning")
runner.tick()
check(len(notices) == 1, "and only once")

# a restart in the middle of a task: it's tried again, not stuck
q.add(queue_file, "Interrupted one")
item = [i for i in q.parse(queue_file.read_text()) if i.text == "Interrupted one"][0]
q.mark(queue_file, item, "~", "working, started 03:00")
now["t"] = datetime(2026, 9, 28, 2, 0)
again = runner.tick()
check(again is not None and again.text == "Interrupted one", "a task left marked as working is picked up again")
runner.running = False

# run now, outside the window
now["t"] = datetime(2026, 9, 28, 15, 0)
q.add(queue_file, "Daytime please")
check(runner.tick() is None, "in the afternoon a task without a time waits for the night")
runner.request_run_now()
item = runner.tick()
check(item is not None, "`toby queue run` starts the queue outside the window")
print("queue checks passed")
