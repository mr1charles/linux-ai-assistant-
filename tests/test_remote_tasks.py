"""Approvals, background jobs, task records and the computer tools.

Runs with HOME pointed at a scratch folder, so the file tools act on real
files without going near yours. What must hold: an approval is answered once
and a no or a timeout never runs anything; phones can't answer computer-only
questions; jobs report real output, progress and exit codes, can be paused,
stopped (with everything they started) and retried; tasks keep their history
across a restart and mark interrupted ones honestly; file tools never delete
outright, back up what they overwrite, and refuse to change anything outside
home; status reports real measurements or None.
"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

scratch = Path(tempfile.mkdtemp())
os.environ["HOME"] = str(scratch)
os.environ["XDG_DATA_HOME"] = str(scratch / ".local" / "share")
os.environ["PATH"] = "/usr/bin:/bin"   # gio may be absent; the manual trash is exercised either way
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import approvals  # noqa: E402
import computer_tools as ct  # noqa: E402
import jobs as jobs_mod  # noqa: E402
import tasks as tasks_mod  # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def check_true(label, got):
    if not got:
        failures.append(f"{label}: expected true, got {got!r}")


# -- approvals ----------------------------------------------------------------------------
changes = []
center = approvals.ApprovalCenter(on_change=lambda: changes.append(1), timeout=5)
aid = center.request("Delete 14 files from ~/Downloads", details=["a.zip"], origin="phone")
check("a question is pending", [a["id"] for a in center.pending()], [aid])
check("and visible to phones", len(center.pending(for_phone=True)), 1)
answer = {}
t = threading.Thread(target=lambda: answer.setdefault("v", center.wait(aid)))
t.start()
time.sleep(0.1)
check("the first answer counts", center.answer(aid, True, by="Jacob's iPhone", from_phone=True), True)
t.join(2)
check("and the waiting task sees yes", answer.get("v"), True)
check("a second answer is ignored", center.answer(aid, False), False)
check("who answered is recorded", center.get(aid)["answered_by"], "Jacob's iPhone")
check("changes are announced", len(changes) >= 2, True)

local = center.request("Pair Stranger's phone?", local_only=True)
check("computer-only questions aren't sent to phones", center.pending(for_phone=True), [])
check("and a phone can't answer them", center.answer(local, True, from_phone=True), False)
check("the computer can", center.answer(local, False), True)
check("a no is a no", center.wait(local), False)

clock = [1000.0]
timed = approvals.ApprovalCenter(clock=lambda: clock[0], timeout=600)
tid = timed.request("Run npm test")
clock[0] += 601
check("an unanswered question counts as no", timed.wait(tid, poll=0.01), False)
check("and is marked expired", timed.get(tid)["status"], "expired")
cid = center.request("Stop steam")
check("cancelling a task answers its question with no", center.wait(cid, cancel_check=lambda: True), False)

# -- jobs ----------------------------------------------------------------------------------
check("percent is read from output", jobs_mod.parse_progress("Building... 72% done"), 72.0)
check("fractions too", jobs_mod.parse_progress("[18/24] Compiling foo"), 75.0)
check("the last of several", jobs_mod.parse_progress("10% 20% 35.5%"), 35.5)
check("nonsense isn't progress", jobs_mod.parse_progress("error 404 at 1234%"), None)
check("plain lines aren't", jobs_mod.parse_progress("hello"), None)

finished = []
manager = jobs_mod.JobManager(on_finish=finished.append, shell=("bash", "-c"))
job = manager.start("echo start; printf '10%%\\r40%%\\r'; echo '[3/4] linking'; echo done", str(scratch))
check_true("a job finishes", job.done.wait(5))
check("successfully", (job.state, job.exit_code), ("succeeded", 0))
check("its output is kept, progress redraws split into lines", job.tail(10),
      ["start", "10%", "40%", "[3/4] linking", "done"])
check("finishing reports it", finished and finished[-1].id, job.id)
check("a finished job with progress is at 100%", job.progress, 100.0)
check_true("it describes itself", "finished successfully" in job.describe())

bad = manager.start("echo oops; exit 3", str(scratch))
bad.done.wait(5)
check("a failing job fails with its exit code", (bad.state, bad.exit_code), ("failed", 3))
check_true("and says so", "exit code 3" in bad.describe())

counter = scratch / "attempts"
flaky = manager.start(f"echo x >> {counter}; exit 1", str(scratch))
manager.watch(flaky.id, on_fail="retry")
flaky.done.wait(5)
time.sleep(0.2)
flaky.done.wait(5)
check("a watched job set to retry runs once more", counter.read_text().count("x"), 2)
check("and then reports the failure", flaky.state, "failed")

slow = manager.start("sleep 30 & wait", str(scratch))
time.sleep(0.2)
check("a running job can be paused", manager.pause(slow.id), True)
check("paused", slow.state, "paused")
check("and resumed", manager.resume(slow.id), True)
check("stopping works", manager.stop(slow.id), True)
check_true("and ends everything it started", slow.done.wait(5))
check("stopped, not failed", slow.state, "stopped")
check("jobs are found by id, by name, or as the latest", (manager.get(job.id) is job,
      manager.get("linking") is job, manager.get("latest") is slow), (True, True, True))
missing = manager.start("true", "/nonexistent/dir")
check("a job in a missing folder fails cleanly", (missing.done.is_set(), missing.state), (True, "failed"))

output, fg = ct.run_command(manager, "echo hello from the test", "~", wait_s=5)
check_true("a foreground command returns its output", "hello from the test" in output)
output, bg = ct.run_command(manager, "sleep 5", "~", wait_s=0.2)
check_true("a slow one carries on in the background", "carries on in the background" in output)
manager.stop(bg.id)

# -- tasks ---------------------------------------------------------------------------------
log_path = scratch / "tasks.json"
log = tasks_mod.TaskLog(log_path)
tid = log.start("check if my project builds", origin="phone:Jacob's iPhone")
check("a task starts out thinking", log.get(tid)["state"], "thinking")
log.set_steps(tid, [("Run the build", "current", "npm run build")])
log.update(tid, state="working")
log.add_file(tid, "~/proj/package.json", "read")
log.add_job(tid, "abcd1234")
check("steps carry details", log.get(tid)["steps"][0]["detail"], "npm run build")
try:
    log.update(tid, state="dancing")
    failures.append("an unknown state is refused")
except ValueError:
    pass
log.finish(tid, "completed", "The build passed.")
running = log.start("start the game")
log.update(running, state="working")
check("recent lists newest first", [t["text"] for t in log.recent()], ["start the game", "check if my project builds"])
check("with progress counts", log.recent()[1]["steps_total"], 1)
log.finish(log.start("x"), "failed", "no")
again = tasks_mod.TaskLog(log_path)
check("history survives a restart", again.get(tid)["reply"], "The build passed.")
check_true("the file is private", oct(log_path.stat().st_mode & 0o777) == "0o600")
reloaded = [t for t in again.recent() if t["text"] == "start the game"]
check("a task interrupted by a restart is marked, not left 'working'",
      reloaded and (reloaded[0]["state"], bool(reloaded[0]["error"])), ("cancelled", True))

# -- computer tools: files ---------------------------------------------------------------------
downloads = scratch / "Downloads" / "Old Projects"
downloads.mkdir(parents=True)
for i in range(3):
    (downloads / f"f{i}.zip").write_text("zip")
(scratch / "notes").mkdir()
listing = ct.list_files("~/Downloads/Old Projects")
check_true("listing names the files", "f0.zip" in listing and "3 items" in listing)
check_true("search finds by name", "~/Downloads/Old Projects/f1.zip" in ct.search_files("f1", "~"))
check_true("search says when nothing matches", "Nothing named like" in ct.search_files("zzzz"))
check_true("writing creates", "Wrote ~/notes/todo.txt" in ct.write_file("~/notes/todo.txt", "one\n"))
result = ct.write_file("~/notes/todo.txt", "two\n")
check_true("overwriting keeps a backup", "old version is saved" in result)
backups = list(ct.BACKUP_DIR.iterdir())
check("the backup has the old contents", backups[0].read_text() if backups else None, "one\n")
ct.write_file("~/notes/todo.txt", "three\n", append=True)
check("appending adds", (scratch / "notes" / "todo.txt").read_text(), "two\nthree\n")
check_true("reading returns the text", "two\nthree" in ct.read_file("~/notes/todo.txt"))
(scratch / "blob.bin").write_bytes(b"\x00\x01\x02" * 10)
check_true("binary files aren't dumped", "binary file" in ct.read_file("~/blob.bin"))
check_true("writing outside home is refused", "only changes files inside your home" in ct.write_file("/tmp/x-toby-test", "x"))
check("and nothing was written", Path("/tmp/x-toby-test").exists(), False)
check_true("renaming", "Renamed" in ct.move_file("~/notes/todo.txt", "~/notes/done.txt"))
(scratch / "notes" / "keep.txt").write_text("keep")
check_true("moving over an existing file is refused",
           "already exists" in ct.move_file("~/notes/done.txt", "~/notes/keep.txt"))
check("so it's untouched", (scratch / "notes" / "keep.txt").read_text(), "keep")
result = ct.trash_files([f"~/Downloads/Old Projects/f{i}.zip" for i in range(3)])
check_true("deleting moves to the trash", "Moved 3 items to the trash" in result)
trash = scratch / ".local" / "share" / "Trash"
check("the files are in the trash, restorable", sorted(p.name for p in (trash / "files").iterdir()),
      ["f0.zip", "f1.zip", "f2.zip"])
check("with their original location recorded", len(list((trash / "info").iterdir())), 3)
check("and gone from where they were", list(downloads.iterdir()), [])
check_true("files touched are recorded", ("~/notes/done.txt", "moved here") in ct.touched())

# -- computer tools: status and programs ----------------------------------------------------------
status = ct.system_status()
check_true("status has the real hostname", bool(status["hostname"]))
check_true("a CPU reading or None, never a made-up number",
           status["cpu_percent"] is None or 0 <= status["cpu_percent"] <= 100)
if Path("/proc/meminfo").exists():
    check_true("memory is measured", 0 < status["memory"]["percent"] <= 100)
check_true("the description reads naturally", status["hostname"] in ct.describe_status(status))
sleeper = manager.start("exec sleep 30", str(scratch))
time.sleep(0.3)
programs = ct.list_programs("sleep")
check_true("running programs are found with how long they've run",
           any(p["name"] == "sleep" and p["running_s"] >= 0 for p in programs))
fake_toby = manager.start("exec -a 'python3 linux_agent_apple.py' sleep 30", str(scratch))
time.sleep(0.3)
check_true("Toby won't stop itself", "Toby itself" in ct.stop_program("linux_agent"))
check("so it's still running", fake_toby.state, "running")
manager.stop(fake_toby.id)
check_true("or the desktop", "desktop depends" in ct.stop_program("hyprland") or "Nothing called" in ct.stop_program("hyprland"))
check_true("stopping a program asks it to quit", "Asked sleep to quit" in ct.stop_program("sleep"))
check_true("and it does", sleeper.done.wait(5))
check("no Hyprland here means no windows, not invented ones", ct.list_windows(), [])

# -- terminals ---------------------------------------------------------------------------------
fake_bin = scratch / "bin"
fake_bin.mkdir()
(fake_bin / "foot").write_text("#!/bin/sh\n")
(fake_bin / "foot").chmod(0o755)
os.environ["PATH"] = f"{fake_bin}:/usr/bin:/bin"
argv = ct.terminal_command(scratch, "claude")
check("a terminal opens in the folder, runs the command, then stays open",
      argv[:3] + argv[3:5], ["foot", "-D", str(scratch), "sh", "-c"])
check_true("ending at your shell", argv[-1].startswith("claude; exec "))

manager.stop_all()
if failures:
    print(f"{len(failures)} PROBLEM(S):")
    for f in failures:
        print("  ", f)
    sys.exit(1)
print("remote task checks passed")
