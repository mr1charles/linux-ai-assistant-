"""
jobs.py — commands Toby runs and keeps an eye on.

"Start building my project", then later, from your phone, "how's the build
going?" — that needs the build to outlive the request that started it. A job
is a command running in the background in its own process group, with its
output kept (the last couple of thousand lines), its progress picked out of
that output when it prints any ("72%", "[18/25]"), and its outcome recorded
when it ends.

A job can be watched: when it finishes, you get a notification, and with
on_fail="retry" a failed job is run once more before Toby gives up and tells
you. Pausing a task pauses its jobs too (SIGSTOP / SIGCONT), and stopping
one ends the whole process group, so nothing it started is left behind.

Jobs live as long as Toby does. If Toby restarts, running jobs end with it,
and their summaries (not their output) survive in the task history.
"""

import collections
import os
import re
import secrets
import signal
import subprocess
import threading
import time

MAX_LINES = 2000
RUNNING, SUCCEEDED, FAILED, STOPPED, PAUSED = "running", "succeeded", "failed", "stopped", "paused"

_PERCENT = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s?%")
_FRACTION = re.compile(r"[\[(](\d+)\s*/\s*(\d+)[\])]")


def parse_progress(line):
    """A percentage from a line of output, if it states one."""
    found = None
    for match in _PERCENT.finditer(line):
        value = float(match.group(1))
        if 0 <= value <= 100:
            found = value
    if found is None:
        match = _FRACTION.search(line)
        if match and int(match.group(2)) > 0 and int(match.group(1)) <= int(match.group(2)):
            found = 100.0 * int(match.group(1)) / int(match.group(2))
    return found


class Job:
    def __init__(self, command, cwd, name=None, task_id=None):
        self.id = secrets.token_hex(4)
        self.command = command
        self.cwd = cwd
        self.name = name or command.split("&&")[0].strip()[:60]
        self.task_id = task_id
        self.state = RUNNING
        self.started = time.time()
        self.ended = None
        self.exit_code = None
        self.progress = None
        self.lines = collections.deque(maxlen=MAX_LINES)
        self.on_fail = "notify"
        self.watched = False
        self.attempts = 1
        self.proc = None
        self.done = threading.Event()

    def summary(self):
        return {"id": self.id, "name": self.name, "command": self.command, "cwd": self.cwd,
                "state": self.state, "progress": self.progress, "started": self.started,
                "ended": self.ended, "exit_code": self.exit_code, "task_id": self.task_id,
                "watched": self.watched, "attempts": self.attempts,
                "last_line": self.lines[-1] if self.lines else ""}

    def tail(self, n=40):
        return list(self.lines)[-n:]

    def describe(self):
        """A sentence for Toby's reply or your phone."""
        took = (self.ended or time.time()) - self.started
        mins, secs = divmod(int(took), 60)
        span = f"{mins}m {secs}s" if mins else f"{secs}s"
        if self.state == RUNNING:
            pct = f", about {self.progress:.0f}% done" if self.progress is not None else ""
            return f"'{self.name}' is still running ({span} so far{pct})."
        if self.state == PAUSED:
            return f"'{self.name}' is paused."
        if self.state == SUCCEEDED:
            return f"'{self.name}' finished successfully in {span}."
        if self.state == STOPPED:
            return f"'{self.name}' was stopped after {span}."
        return f"'{self.name}' failed (exit code {self.exit_code}) after {span}."


class JobManager:
    """Starts, tracks and stops jobs. on_finish(job) runs when one ends."""

    def __init__(self, on_finish=None, on_change=None, shell=("bash", "-lc")):
        self.on_finish = on_finish
        self.on_change = on_change
        self.shell = list(shell)
        self._jobs = collections.OrderedDict()
        self._lock = threading.Lock()

    def start(self, command, cwd=None, name=None, task_id=None, env=None):
        job = Job(command, cwd or os.path.expanduser("~"), name, task_id)
        self._launch(job, env)
        with self._lock:
            self._jobs[job.id] = job
            while len(self._jobs) > 30:
                oldest = next(iter(self._jobs))
                if self._jobs[oldest].state in (RUNNING, PAUSED):
                    break
                self._jobs.popitem(last=False)
        self._changed()
        return job

    def _launch(self, job, env=None):
        run_env = dict(os.environ, **(env or {}))
        run_env.setdefault("TERM", "dumb")
        run_env["CI"] = run_env.get("CI", "1")   # most tools print plainer progress for CI
        try:
            job.proc = subprocess.Popen(self.shell + [job.command], cwd=job.cwd, env=run_env,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, start_new_session=True)
        except (OSError, ValueError) as e:
            job.lines.append(f"Couldn't start: {e}")
            job.state, job.exit_code, job.ended = FAILED, -1, time.time()
            job.done.set()
            return
        threading.Thread(target=self._read, args=(job,), daemon=True, name=f"job-{job.id}").start()

    def _read(self, job):
        proc = job.proc
        buffer = b""
        last_change = 0.0
        while True:
            chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
            if not chunk:
                break
            buffer += chunk
            # progress bars redraw with \r; treat each redraw as a line
            parts = re.split(rb"\r\n|\n|\r", buffer)
            buffer = parts.pop()
            for raw in parts:
                line = raw.decode("utf-8", "replace").rstrip()
                line = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", line)   # colour codes
                if not line.strip():
                    continue
                job.lines.append(line[:500])
                pct = parse_progress(line)
                if pct is not None:
                    job.progress = pct
            if time.monotonic() - last_change > 0.5:
                last_change = time.monotonic()
                self._changed()
        if buffer.strip():
            job.lines.append(buffer.decode("utf-8", "replace").rstrip()[:500])
        code = proc.wait()
        job.exit_code = code
        job.ended = time.time()
        if job.state != STOPPED:
            job.state = SUCCEEDED if code == 0 else FAILED
        if job.state == SUCCEEDED:
            job.progress = 100.0 if job.progress is not None else None
        if job.state == FAILED and job.watched and job.on_fail == "retry" and job.attempts < 2:
            job.attempts += 1
            job.lines.append(f"--- failed with exit code {code}; trying once more ---")
            job.state, job.ended, job.exit_code, job.progress = RUNNING, None, None, None
            self._changed()
            self._launch(job)
            return
        job.done.set()
        self._changed()
        if self.on_finish:
            try:
                self.on_finish(job)
            except Exception as e:
                print("JOB FINISH ERROR:", e, flush=True)

    # -- queries ------------------------------------------------------------------------
    def get(self, job_id):
        with self._lock:
            if job_id in (None, "", "latest", "last"):
                return next(reversed(self._jobs.values()), None) if self._jobs else None
            if job_id in self._jobs:
                return self._jobs[job_id]
            # the model may name a job instead of quoting its id
            for job in reversed(list(self._jobs.values())):
                if str(job_id).lower() in job.name.lower() or str(job_id).lower() in job.command.lower():
                    return job
        return None

    def all(self):
        with self._lock:
            return list(self._jobs.values())

    def running(self):
        return [j for j in self.all() if j.state in (RUNNING, PAUSED)]

    # -- control ------------------------------------------------------------------------
    def watch(self, job_id, on_fail="notify"):
        job = self.get(job_id)
        if job is None:
            return None
        job.watched = True
        job.on_fail = on_fail if on_fail in ("notify", "retry") else "notify"
        self._changed()
        return job

    def stop(self, job_id):
        job = self.get(job_id)
        if job is None or job.state not in (RUNNING, PAUSED) or job.proc is None:
            return False
        job.state = STOPPED
        self._signal(job, signal.SIGCONT)
        self._signal(job, signal.SIGTERM)

        def kill_later():
            if not job.done.wait(3):
                self._signal(job, signal.SIGKILL)
        threading.Thread(target=kill_later, daemon=True).start()
        self._changed()
        return True

    def pause(self, job_id):
        job = self.get(job_id)
        if job is None or job.state != RUNNING:
            return False
        if self._signal(job, signal.SIGSTOP):
            job.state = PAUSED
            self._changed()
            return True
        return False

    def resume(self, job_id):
        job = self.get(job_id)
        if job is None or job.state != PAUSED:
            return False
        if self._signal(job, signal.SIGCONT):
            job.state = RUNNING
            self._changed()
            return True
        return False

    def stop_all(self):
        for job in self.running():
            self.stop(job.id)

    @staticmethod
    def _signal(job, sig):
        try:
            os.killpg(os.getpgid(job.proc.pid), sig)
            return True
        except (ProcessLookupError, PermissionError, AttributeError):
            return False

    def _changed(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception as e:
                print("JOB CHANGE ERROR:", e, flush=True)
