"""
tasks.py — what you asked Toby to do, and how it went.

Each request becomes a task with a state your phone can show at a glance:

  thinking          working out what to do
  preparing         about to act (Toby is getting into position)
  working           doing the steps
  waiting           a step is waiting on something slow (a build, a download)
  needs_permission  waiting for your yes, on the computer or your phone
  paused            you paused it; nothing more happens until you resume
  completed / failed / cancelled

A task keeps its steps (with a short detail for each), the files it touched,
the jobs it started and Toby's reply. The last 30 are kept in tasks.json so
"continue what I was doing earlier" and the phone's Tasks tab have something
real to go on after a restart. Output from commands is not stored here —
only in the running job, and only for as long as Toby runs.
"""

import json
import os
import secrets
import threading
import time
from pathlib import Path

TASKS_PATH = Path.home() / "linux-agent" / "tasks.json"
STATES = ("thinking", "preparing", "working", "waiting", "needs_permission", "paused",
          "completed", "failed", "cancelled")
FINISHED = ("completed", "failed", "cancelled")
KEEP = 30


class TaskLog:
    def __init__(self, path=None, clock=time.time, keep=KEEP):
        self.path = Path(path or TASKS_PATH)
        self.clock = clock
        self.keep = keep
        self._lock = threading.RLock()
        self._tasks = []
        self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, list):
                self._tasks = [t for t in data if isinstance(t, dict) and "id" in t][-self.keep:]
        except (OSError, ValueError):
            self._tasks = []
        # anything that was mid-flight when Toby stopped didn't finish
        for t in self._tasks:
            if t.get("state") not in FINISHED:
                t["state"] = "cancelled"
                t.setdefault("reply", "")
                t["error"] = "Toby stopped before this finished."

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self._tasks[-self.keep:], f)
            os.replace(tmp, self.path)
        except OSError as e:
            print("TASK LOG ERROR:", e, flush=True)

    def start(self, text, origin="computer"):
        now = self.clock()
        task = {"id": secrets.token_hex(5), "text": str(text)[:300], "origin": origin,
                "state": "thinking", "created": now, "updated": now, "ended": None,
                "steps": [], "reply": "", "files": [], "jobs": [], "error": None}
        with self._lock:
            self._tasks.append(task)
            self._tasks = self._tasks[-self.keep:]
        return task["id"]

    def update(self, task_id, **fields):
        with self._lock:
            task = self._find(task_id)
            if task is None:
                return None
            if "state" in fields and fields["state"] not in STATES:
                raise ValueError(f"unknown task state {fields['state']!r}")
            task.update(fields)
            task["updated"] = self.clock()
            return dict(task)

    def set_steps(self, task_id, steps):
        """steps: [(label, status)] or [{"label", "status", "detail"}]."""
        clean = []
        for s in steps:
            if isinstance(s, dict):
                clean.append({"label": str(s.get("label", ""))[:120], "status": s.get("status", "pending"),
                              "detail": str(s.get("detail", "") or "")[:300]})
            else:
                label, status = s[0], s[1]
                detail = s[2] if len(s) > 2 else ""
                clean.append({"label": str(label)[:120], "status": status, "detail": str(detail or "")[:300]})
        return self.update(task_id, steps=clean)

    def add_file(self, task_id, path, action):
        with self._lock:
            task = self._find(task_id)
            if task is None:
                return
            entry = {"path": str(path), "action": action, "at": self.clock()}
            task["files"] = ([f for f in task["files"] if f["path"] != entry["path"]] + [entry])[-50:]

    def add_job(self, task_id, job_id):
        with self._lock:
            task = self._find(task_id)
            if task is not None and job_id not in task["jobs"]:
                task["jobs"].append(job_id)

    def finish(self, task_id, state, reply="", error=None):
        if state not in FINISHED:
            raise ValueError(f"{state!r} isn't a finished state")
        with self._lock:
            task = self.update(task_id, state=state, reply=str(reply)[:2000], error=error,
                               ended=self.clock())
            self._save()
            return task

    def get(self, task_id):
        with self._lock:
            task = self._find(task_id)
            return json.loads(json.dumps(task)) if task else None

    def recent(self, n=20):
        """Newest first, without the step details, for lists."""
        with self._lock:
            out = []
            for t in reversed(self._tasks[-n:]):
                done = sum(1 for s in t["steps"] if s["status"] == "done")
                out.append({k: t[k] for k in ("id", "text", "origin", "state", "created", "updated", "ended",
                                              "reply", "error")}
                           | {"steps_done": done, "steps_total": len(t["steps"])})
            return out

    def last_finished(self):
        with self._lock:
            for t in reversed(self._tasks):
                if t["state"] in FINISHED:
                    return dict(t)
        return None

    def _find(self, task_id):
        for t in self._tasks:
            if t["id"] == task_id:
                return t
        return None
