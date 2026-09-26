"""
task_queue.py — things for Toby to do while you sleep.

Write tasks in a Markdown checklist (inspired by Jarvis's batch runner):
Toby/Queue.md in your notes folder if you've set one (so you can add to it
from your phone in Obsidian), otherwise ~/linux-agent/Queue.md. Or tell
Toby "tonight, …", or send /queue … on Telegram.

    - [ ] Find the five biggest folders in ~/Downloads and list them
    - [ ] Read ~/project/README.md and write me a summary of what's left to do
      Keep it short, in bullet points.
    - [ ] at 14:30: check whether ~/project builds

Indented lines under a task are part of it. A task starting "at HH:MM:" runs
at that time (the next time the clock reaches it after the task was
written); the others run in the night window, 01:00 to 07:00 by default.
Toby only starts one when it isn't busy, and only while the computer is
awake: a laptop with its lid shut is asleep, and the tasks wait for the next
night (or `toby queue run`). While it works it asks the system not to
idle-suspend.

Nobody's there to say yes at night, so a task that wants to do something
that would ask first (change or delete files, run commands, use the mouse)
doesn't do it: it's marked "needs you" with what it wanted, for the morning.
Looking, reading, searching your notes and writing its own notes all work.

Each finished task is ticked in the file, [x] done or [!] needs you or
couldn't finish, and what Toby found goes in Outbox/<date> <task>.md next to
the queue. In the morning you get one notification with the tally.
"""

import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

STATE_PATH = Path.home() / "linux-agent" / "queue_state.json"
HEADER = """# Toby's queue

Write tasks here, one per line, and Toby works through them at night while
the computer is awake (01:00 to 07:00), then ticks them off and puts what it
found in Outbox/. Start a task with "at 14:30:" to have it done at that time
instead. Things that need your OK are left for you, not done.

"""

_ITEM = re.compile(r"^(\s*)[-*]\s+\[([ xX!~])\]\s+(.*\S)\s*$")
_AT = re.compile(r"^at\s+(\d{1,2})[:.](\d{2})\s*[:,-]?\s*(.*)$", re.I)
_NOTE = re.compile(r"\s+\((?:done|needs you|couldn't finish|working)\b[^()]*(?:\([^()]*\)[^()]*)*\)\s*$")


class Item:
    def __init__(self, line, state, text, at, body):
        self.line = line          # index of its line in the file
        self.state = state        # " " to do, "x" done, "!" needs you / failed, "~" working
        self.text = text          # the task as written, without our note
        self.at = at              # (hour, minute) or None
        self.body = body          # indented lines under it

    @property
    def prompt(self):
        task = _AT.sub(lambda m: m.group(3), self.text).strip() if self.at else self.text
        return " ".join([task] + self.body)      # one line: it's typed into Toby like anything else

    @property
    def title(self):
        text = _AT.sub(lambda m: m.group(3), self.text).strip() if self.at else self.text
        return text[:70]

    @property
    def key(self):
        return hashlib.sha1(self.text.encode()).hexdigest()[:12]

    def __repr__(self):
        return f"Item({self.state!r}, {self.text!r}, at={self.at})"


def parse(text):
    lines = text.splitlines()
    items, current = [], None
    for i, line in enumerate(lines):
        match = _ITEM.match(line)
        if match and len(match.group(1)) < 2:
            task = _NOTE.sub("", match.group(3)).strip()
            at_match = _AT.match(task)
            at = None
            if at_match and int(at_match.group(1)) < 24 and int(at_match.group(2)) < 60:
                at = (int(at_match.group(1)), int(at_match.group(2)))
            current = Item(i, match.group(2).lower(), task, at, [])
            items.append(current)
        elif current is not None and line.strip() and (line.startswith("  ") or line.startswith("\t")):
            current.body.append(line.strip())
        elif not line.strip():
            continue
        else:
            current = None
    return items


def queue_path(notes_root=None):
    if notes_root is not None:
        return Path(notes_root) / "Toby" / "Queue.md"
    return Path.home() / "linux-agent" / "Queue.md"


def ensure(path):
    path = Path(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(HEADER)
    return path


def add(path, text, at=None):
    """Add a task to the end of the queue. Returns its line."""
    text = " ".join(str(text or "").split())
    if not text:
        raise ValueError("say what to do")
    if at:
        text = f"at {at}: {text}"
    path = ensure(path)
    content = path.read_text()
    if content and not content.endswith("\n"):
        content += "\n"
    path.write_text(content + f"- [ ] {text}\n")
    return text


def mark(path, item, state, note):
    """Tick a task, with a short note after it. The file is re-read first, and
    the task found again by its words, in case you edited it meanwhile."""
    path = Path(path)
    lines = path.read_text().splitlines()
    index = None
    if item.line < len(lines):
        m = _ITEM.match(lines[item.line])
        if m and _NOTE.sub("", m.group(3)).strip() == item.text:
            index = item.line
    if index is None:
        for i, line in enumerate(lines):
            m = _ITEM.match(line)
            if m and _NOTE.sub("", m.group(3)).strip() == item.text:
                index = i
                break
    if index is None:
        return False          # you deleted it; nothing to tick
    indent = _ITEM.match(lines[index]).group(1)
    lines[index] = f"{indent}- [{state}] {item.text}" + (f" ({note})" if note else "")
    path.write_text("\n".join(lines) + "\n")
    return True


# ---------------------------------------------------------------------------
# When things are due
# ---------------------------------------------------------------------------

def load_state(path=None):
    try:
        data = json.loads(Path(path or STATE_PATH).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state, path=None):
    path = Path(path or STATE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1))


def parse_hhmm(text, default):
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(text or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else default


def in_window(now, start, end):
    minutes = now.hour * 60 + now.minute
    a, b = start[0] * 60 + start[1], end[0] * 60 + end[1]
    return a <= minutes < b if a < b else (minutes >= a or minutes < b)


def due_time(item, seen):
    """For a task with a time: the first time the clock reads it after the task
    was first seen."""
    first = datetime.fromtimestamp(seen)
    at = first.replace(hour=item.at[0], minute=item.at[1], second=0, microsecond=0)
    return at if at > first else at + timedelta(days=1)


def next_due(items, now, state, window=((1, 0), (7, 0)), run_now=False):
    """The next task to start, or None. Notes when each task was first seen
    (in state) so "at 03:00" written at 22:00 means tonight, not already late."""
    for item in items:
        state.setdefault(item.key, {"seen": now.timestamp()})
    for item in items:
        if item.state != " ":
            continue
        if item.at:
            if now >= due_time(item, state[item.key]["seen"]) or run_now:
                return item
        elif run_now or in_window(now, *window):
            return item
    return None


def outbox_note(outbox, item, reply, record, now=None):
    """Write what Toby found for one task. Returns the file."""
    now = now or datetime.now()
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", " ", item.title).strip(" .")[:60] or "Task"
    path = Path(outbox) / f"{now:%Y-%m-%d} {name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    state = (record or {}).get("state", "")
    lines = [f"# {item.title}", "", f"*{now:%A %d %B %Y, %H:%M}. "
             + {"completed": "Done.", "failed": "Couldn't finish.", "cancelled": "Stopped."}.get(state, "") + "*",
             "", "## What you asked", "", item.prompt, "", "## What Toby found", "", (reply or "(no reply)").strip()]
    steps = (record or {}).get("steps") or []
    if steps:
        lines += ["", "## Steps", ""]
        marks = {"done": "x", "error": "!", "current": " ", "pending": " "}
        for step in steps:
            detail = f": {step['detail']}" if step.get("detail") else ""
            lines.append(f"- [{marks.get(step.get('status'), ' ')}] {step.get('label', '')}{detail}")
    left = (record or {}).get("left_for_you") or []
    if left:
        lines += ["", "## Left for you", "", "These need your OK, so Toby didn't do them:"]
        lines += [f"- {what}" for what in left]
    files = (record or {}).get("files") or []
    if files:
        lines += ["", "## Files", ""] + [f"- {f.get('action', '')}: {f.get('path', '')}" for f in files]
    path.write_text("\n".join(lines) + "\n")
    return path


def tally(results):
    """One line for the morning notification."""
    done = sum(1 for r in results if r == "done")
    needs = sum(1 for r in results if r == "needs you")
    failed = sum(1 for r in results if r == "failed")
    parts = [f"{done} done"] if done else []
    if needs:
        parts.append(f"{needs} need{'s' if needs == 1 else ''} you")
    if failed:
        parts.append(f"{failed} couldn't be finished")
    return ", ".join(parts) or "nothing to report"


class QueueRunner:
    """Decides when to start the next queued task and records how it went.

    ask(prompt) runs one task the normal way and blocks until it's finished,
    returning the task's record (state, reply, steps, files, left_for_you).
    It's called on a background thread. Everything else is quick.
    """

    def __init__(self, path_fn, ask, busy, settings, notify, inhibit=None, clock=datetime.now,
                 state_path=None):
        self.path_fn = path_fn
        self.ask = ask
        self.busy = busy
        self.settings = settings
        self.notify = notify
        self.inhibit = inhibit or (lambda: None)
        self.clock = clock
        self.state_path = state_path
        self.running = False
        self.results = []          # this night's outcomes, for the morning summary
        self.run_now_flag = Path(str(state_path or STATE_PATH) + ".run-now")

    def window(self):
        s = self.settings()
        return (parse_hhmm(s.get("queue_start"), (1, 0)), parse_hhmm(s.get("queue_end"), (7, 0)))

    def request_run_now(self):
        self.run_now_flag.parent.mkdir(parents=True, exist_ok=True)
        self.run_now_flag.touch()

    def tick(self):
        """Called every so often. Returns the item it started, or None."""
        if not self.settings().get("queue_enabled", True):
            return None
        now = self.clock()
        self._maybe_summarise(now)
        if self.running or self.busy():
            return None
        path = self.path_fn()
        if not path.exists():
            return None
        run_now = self.run_now_flag.exists()
        items = parse(path.read_text())
        for item in items:
            if item.state == "~":                  # Toby stopped mid-task (a restart): try again
                mark(path, item, " ", "")
                item.state = " "
        state = load_state(self.state_path)
        item = next_due(items, now, state, self.window(), run_now)
        keys = {i.key for i in items}
        save_state({k: v for k, v in state.items() if k in keys or k == "_summary"}, self.state_path)
        if item is None:
            if run_now:
                self.run_now_flag.unlink(missing_ok=True)
            return None
        self.running = True
        return item

    def run(self, item):
        """Do one task (blocking) and record it."""
        path = self.path_fn()
        release = self.inhibit()
        try:
            mark(path, item, "~", f"working, started {self.clock():%H:%M}")
            try:
                record = self.ask(item.prompt) or {}
            except Exception as e:  # never leave a task marked as working
                record = {"state": "failed", "reply": f"Toby hit a problem: {e}"}
            reply = record.get("reply") or ""
            outbox = outbox_note(path.parent / "Outbox", item, reply, record, self.clock())
            where = os.path.relpath(outbox, path.parent)
            when = f"{self.clock():%H:%M}"
            if record.get("left_for_you"):
                outcome, tick_mark, note = "needs you", "!", f"needs you: {record['left_for_you'][0][:60]}; see {where}"
            elif record.get("state") == "completed":
                outcome, tick_mark, note = "done", "x", f"done {when}, see {where}"
            else:
                outcome, tick_mark, note = "failed", "!", f"couldn't finish, {when}; see {where}"
            mark(path, item, tick_mark, note)
            self.results.append(outcome)
            state = load_state(self.state_path)
            state.setdefault("_summary", {"results": [], "due": None})
            state["_summary"]["results"].append(outcome)
            start, end = self.window()
            now = self.clock()
            due = now.replace(hour=end[0], minute=end[1], second=0, microsecond=0)
            if not in_window(now, start, end):
                due = now                          # not a night run: say so straight away
            elif due < now:
                due += timedelta(days=1)
            state["_summary"]["due"] = due.timestamp()
            save_state(state, self.state_path)
            return outcome
        finally:
            if release:
                release()
            self.running = False

    def _maybe_summarise(self, now):
        state = load_state(self.state_path)
        summary = state.get("_summary")
        if not summary or summary.get("due") is None or now.timestamp() < summary["due"]:
            return
        if self.running:
            return
        results = summary.get("results") or []
        state.pop("_summary", None)
        save_state(state, self.state_path)
        if results:
            self.notify("From your queue: " + tally(results), f"Details are in {self.path_fn().parent / 'Outbox'}")
