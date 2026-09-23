#!/usr/bin/env python3
"""A real Toby bridge for the iPhone app's UI tests, on the build machine.

This runs the actual scripts/remote_bridge.py server, device store, pairing,
approvals, jobs and task log. Two things are test doubles, and only these:

- The AI. There is no language model on a CI machine, so a request is
  answered by a fixed script: it plans "Run npm test", asks the phone for
  permission through the real approval queue, runs a real command through
  the real job manager (it prints five test lines), and replies with what
  that command actually printed.
- The person at the computer. Pairing claims are approved automatically
  after two seconds, so the test can photograph the comparison screen. The
  pairing code is fixed (from TOBY_PAIR_CODE) and reusable, so each test
  run can pair a freshly reset app.

Status comes from computer_tools.system_status() on the build machine
itself, so values it can't measure there (no /proc on macOS) are really
absent, and the app's "not reported" states get exercised.

Nothing here is used by the real app.
"""

import argparse
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import approvals  # noqa: E402
import computer_tools  # noqa: E402
import jobs as jobs_mod  # noqa: E402
import remote_bridge as rb  # noqa: E402
import tasks as tasks_mod  # noqa: E402


class FixedCodePairing(rb.PairingCenter):
    """A pairing center whose code is fixed and never used up."""

    def __init__(self, store, code):
        super().__init__(store)
        self.fixed = rb.normalize_code(code)

    def claim(self, code, device_name, device_id, platform):
        with self._cond:
            self._sessions = {"test": {"id": "test", "code": self.fixed, "expires": self.clock() + 300,
                                       "claimed": False, "cancelled": False}}
        return super().claim(code, device_name, device_id, platform)


class ScriptedToby:
    """The computer side, with a fixed script standing in for the model."""

    def __init__(self, workdir):
        self.lock = threading.RLock()
        self.busy = False
        self.reply = ""
        self.history = []
        self.task_id = None
        self.steps = []
        self.work_mode = False
        self.tasks = tasks_mod.TaskLog(Path(workdir) / "tasks.json")
        self.jobs = jobs_mod.JobManager(on_change=lambda: self.publish())
        self.approvals = approvals.ApprovalCenter(on_change=lambda: self.publish(), timeout=120)
        self.project = Path(workdir) / "project"
        self.project.mkdir(exist_ok=True)
        self.bridge = None

    # -- what the phone sees ---------------------------------------------------------
    def publish(self):
        if self.bridge is None:
            return
        with self.lock:
            record = self.tasks.get(self.task_id) if self.task_id else None
            task = None
            if record:
                task = {k: record[k] for k in ("id", "text", "origin", "state", "created", "ended", "error")}
                task["steps"] = list(self.steps)
                task["reply"] = self.reply
            pending = [{k: a[k] for k in ("id", "kind", "level", "title", "details", "reason", "created", "expires")}
                       for a in self.approvals.pending(for_phone=True)]
            self.bridge.publish({
                "busy": self.busy, "task": task,
                "steps": [{"label": s["label"], "status": s["status"]} for s in self.steps],
                "reply": self.reply, "approvals": pending, "history": self.history[-8:],
                "confirm": {"pending": True, "text": pending[0]["title"]} if pending else None,
                "jobs": [j.summary() for j in reversed(self.jobs.all())][:8],
                "work_mode": self.work_mode, "power": "awake", "screen_view": False, "viewing": False,
            })

    def set_state(self, state):
        self.tasks.update(self.task_id, state=state)
        self.publish()

    # -- the scripted task -------------------------------------------------------------
    def run(self, text, origin):
        command = "for i in 1 2 3 4 5; do echo \"[$i/5] test_$i ok\"; sleep 0.3; done; echo '5 tests passed'"
        time.sleep(1.0)                                   # "thinking"
        with self.lock:
            self.steps = [{"label": "Run npm test", "status": "current", "detail": ""}]
        self.set_state("working")
        self.set_state("needs_permission")
        self.bridge.events.add("needs_permission", "Toby needs your OK", "Run: npm test")
        allowed = self.approvals.ask("Run: npm test", level="confirm", details=[f"In {self.project}"],
                                     reason="running a command", tool="run_command", origin=origin)
        if not allowed:
            with self.lock:
                self.steps[0].update(status="error", detail="You said no, so Toby stopped here.")
                self.reply = "Okay, I didn't run the tests."
            self.finish("cancelled")
            return
        self.set_state("waiting")
        output, job = computer_tools.run_command(self.jobs, command, str(self.project), wait_s=30,
                                                 task_id=self.task_id, name="npm test")
        self.tasks.add_job(self.task_id, job.id)
        last = job.tail(1)[0] if job.tail(1) else ""
        with self.lock:
            self.steps[0].update(status="done", detail=job.describe())
            self.reply = f"All good: the command finished and said \"{last}\"." if job.state == "succeeded" \
                else f"The tests didn't pass: {last}"
        self.finish("completed" if job.state == "succeeded" else "failed")

    def finish(self, state):
        with self.lock:
            self.tasks.set_steps(self.task_id, self.steps)
            self.tasks.add_file(self.task_id, str(self.project / "package.json"), "read")
            self.tasks.finish(self.task_id, state, self.reply)
            self.history.append({"role": "assistant", "content": self.reply})
            self.busy = False
        self.bridge.events.add("task_done" if state == "completed" else "task_failed",
                               "Done: run the tests", self.reply)
        self.publish()

    # -- the services the bridge calls -------------------------------------------------
    def ask(self, text, device):
        with self.lock:
            if self.busy:
                return False, "Toby is still working on the last thing.", None
            self.busy = True
            origin = f"phone:{device['name']}"
            self.task_id = self.tasks.start(text, origin)
            self.steps = []
            self.reply = ""
            self.history.append({"role": "user", "content": text})
        self.publish()
        threading.Thread(target=self.run, args=(text, origin), daemon=True).start()
        return True, "Sent.", self.task_id

    def approve(self, approval_id, allow, device):
        return self.approvals.answer(approval_id, allow, by=device["name"], from_phone=True)

    def task_control(self, action, device):
        return False, "The test computer doesn't pause."

    def status(self):
        status = computer_tools.system_status()
        status["toby"] = {"busy": self.busy, "model": "scripted (test)", "power": "awake",
                          "current_task": None, "work_mode": self.work_mode}
        status["policy"] = {"screen_view": False, "restricted_actions": False}
        status["jobs_running"] = len(self.jobs.running())
        return status

    def task(self, task_id):
        task = self.tasks.get(task_id)
        if task:
            task["job_details"] = [j.summary() for j in (self.jobs.get(i) for i in task["jobs"]) if j]
        return task

    def jobs_list(self):
        return [j.summary() for j in reversed(self.jobs.all())]

    def job(self, job_id, lines):
        job = self.jobs.get(job_id)
        return None if job is None else dict(job.summary(), output=job.tail(lines))

    def screen(self, view, max_width):
        return None, "screen_view_off"

    def overview(self):
        return {"windows": computer_tools.list_windows(), "screen": None, "screen_view": False}

    def files(self):
        last = self.tasks.last_finished()
        return last.get("files", []) if last else []

    def focus_window(self, address, device):
        return False, "The test computer has no windows."

    def set_work_mode(self, on, device):
        self.work_mode = on
        self.publish()
        return True, "Work Mode is on." if on else "Work Mode is off."


class Services:
    """Adapts ScriptedToby to the bridge's method names (tasks/jobs are
    attributes there, so the list calls are renamed)."""

    def __init__(self, toby):
        self.t = toby

    def __getattr__(self, name):
        return {"tasks": self.t.tasks.recent, "jobs": self.t.jobs_list}.get(name) or getattr(self.t, name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--code", default=os.environ.get("TOBY_PAIR_CODE", "K7M4XQ2P"))
    args = parser.parse_args()
    workdir = tempfile.mkdtemp(prefix="toby-ci-")
    toby = ScriptedToby(workdir)
    store = rb.DeviceStore(Path(workdir) / "remote.json")
    pairing = FixedCodePairing(store, args.code)

    def approve_later(claim):
        def go():
            time.sleep(2.0)
            pairing.decide(claim["id"], True)
            print(f"approved pairing for {claim['name']} (number {claim['compare']})", flush=True)
        threading.Thread(target=go, daemon=True).start()
    pairing.on_claim = approve_later

    bridge = rb.RemoteBridge(Services(toby), store, host="127.0.0.1", port=args.port,
                             name="Test Laptop", pairing=pairing)
    toby.bridge = bridge
    bridge.start()
    toby.publish()
    print(f"test Toby listening on http://127.0.0.1:{args.port} (code {args.code})", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        bridge.stop()


if __name__ == "__main__":
    main()
