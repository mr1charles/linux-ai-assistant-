"""The phone's screen view and notification delivery, with grim, hyprctl and
the network replaced by recorders. What must hold: the Toby view captures
just the focused window; captures are scaled to fit a phone and rate-limited;
a missing grim or a failed capture says so instead of returning something
stale; viewing is noticed (so the computer can show it); ntfy is off unless
configured, only sends to https (or a local/tailnet address), respects the
categories you turned off, and never sends more than a title and a line."""
import os
import subprocess
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import notify  # noqa: E402
import screen_view  # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


calls = []


def runner(args, capture_output=True, text=False, timeout=None):
    calls.append(args)
    if args[:2] == ["hyprctl", "-j"]:
        if args[2] == "activewindow":
            return subprocess.CompletedProcess(args, 0, '{"at": [100, 50], "size": [800, 600]}', "")
        return subprocess.CompletedProcess(args, 0, '[{"focused": true, "width": 2560, "scale": 1.25}]', "")
    if args[0] == "grim":
        return subprocess.CompletedProcess(args, 0, b"\xff\xd8JPEG", b"")
    return subprocess.CompletedProcess(args, 1, "", "")


screen_view.shutil = types.SimpleNamespace(which=lambda name: "/usr/bin/" + name)
clock = Clock()
viewer = screen_view.ScreenViewer(runner=runner, clock=clock)
check("nobody is viewing at first", bool(viewer.viewing()), False)
image, reason = viewer.capture("full", 1024)
check("a full capture returns the JPEG", (image, reason), (b"\xff\xd8JPEG", None))
grim = [c for c in calls if c[0] == "grim"][-1]
check("scaled to the phone's width (2560 at 1.25x is 2048 wide)", grim[grim.index("-s") + 1], "0.500")
check("as JPEG to stdout", (grim[1:3], grim[-1]), (["-t", "jpeg"], "-"))
check("the full view has no region", "-g" in grim, False)
check("looking is noticed", bool(viewer.viewing()), True)
calls.clear()
again, _ = viewer.capture("full", 1024)
check("a second look straight away reuses the capture", ([c for c in calls if c[0] == "grim"], again), ([], image))
clock.now += 1
viewer.capture("toby", 1170)
grim = [c for c in calls if c[0] == "grim"][-1]
check("the Toby view captures just the focused window", grim[grim.index("-g") + 1], "100,50 800x600")
check("a window narrower than the phone isn't enlarged", grim[grim.index("-s") + 1], "1.000")
clock.now += 10
check("viewing stops being reported once the phone stops looking", bool(viewer.viewing()), False)


def failing(args, **kw):
    if args[0] == "grim":
        return subprocess.CompletedProcess(args, 1, b"", b"no output")
    return runner(args, **kw)


broken = screen_view.ScreenViewer(runner=failing, clock=clock)
check("a failed capture says so", broken.capture("full"), (None, "capture_failed"))
screen_view.shutil = types.SimpleNamespace(which=lambda name: None)
check("without grim it says so", screen_view.ScreenViewer(runner=runner).capture("full"), (None, "grim_missing"))

# -- ntfy -------------------------------------------------------------------------------
sent = []


class Opener:
    def __call__(self, req, timeout=None):
        sent.append(req)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


event = {"kind": "job_done", "title": "Your build finished", "body": "npm run build succeeded " + "x" * 400}
for url in ("", "http://ntfy.example.com/topic", "ftp://x/y"):
    check(f"{url!r} isn't a place Toby sends to", notify.NtfySender(url, dict, Opener())(event), False)
check("https is fine", notify.NtfySender.valid("https://ntfy.sh/toby-abc"), True)
check("so is a tailnet address", notify.NtfySender.valid("http://pi.tail1234.ts.net/toby"), True)
sender = notify.NtfySender("https://ntfy.sh/toby-abc", lambda: {}, Opener())
sender._send(event)
req = sent[-1]
check("the title goes in the header", req.get_header("Title"), "Your build finished")
check("the body is one short line", len(req.data) <= 300, True)
check("a turned-off category isn't sent",
      notify.NtfySender("https://ntfy.sh/t", lambda: {"notify_categories": {"job_done": False}}, Opener())(event), False)
check("categories are on by default", notify.wanted("needs_permission", {}), True)
sender._send({"kind": "needs_permission", "title": "Toby needs your OK", "body": "Run npm test"})
check("asking for permission is sent at high priority", sent[-1].get_header("Priority"), "high")

if failures:
    print(f"{len(failures)} PROBLEM(S):")
    for f in failures:
        print("  ", f)
    sys.exit(1)
print("screen view and notification checks passed")
