"""Checks on the Hyprland event listener: parsing, coalescing bursts, and
surviving the compositor going away and coming back."""
import os
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import hypr_events as he  # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


check("a normal line parses", he.parse_line("workspace>>3"), ("workspace", "3"))
check("data may contain >>", he.parse_line("activewindow>>kitty,a>>b"), ("activewindow", "kitty,a>>b"))
check("junk is ignored", he.parse_line("not an event"), None)
check("an empty name is ignored", he.parse_line(">>3"), None)

runtime = tempfile.mkdtemp()
env = {"HYPRLAND_INSTANCE_SIGNATURE": "abc", "XDG_RUNTIME_DIR": runtime}
check("with nothing running, the configured instance is assumed", he.socket_path(env),
      os.path.join(runtime, "hypr", "abc", ".socket2.sock"))
check("no Hyprland at all, no path", he.socket_path({"XDG_RUNTIME_DIR": runtime}), None)
# Hyprland restarted: our environment names a dead instance, a new one exists
os.makedirs(os.path.join(runtime, "hypr", "new_instance"))
open(os.path.join(runtime, "hypr", "new_instance", ".socket2.sock"), "w").close()
check("after a restart the new instance is found", he.socket_path(env),
      os.path.join(runtime, "hypr", "new_instance", ".socket2.sock"))

now = [0.0]
c = he.Coalescer(0.35, clock=lambda: now[0])
check("first event passes", c.allow("workspace"), True)
check("a burst is collapsed", c.allow("workspace"), False)
check("other kinds are independent", c.allow("openwindow"), True)
now[0] += 0.4
check("after the interval it passes again", c.allow("workspace"), True)

# A fake Hyprland: a real UNIX socket that sends events, disappears, and
# comes back — the listener must follow it across the restart.
workdir = tempfile.mkdtemp()
path = os.path.join(workdir, ".socket2.sock")
received = []
got_second = threading.Event()


def fake_instance(lines):
    """Bind now (before anyone looks), serve one client on a thread."""
    if os.path.exists(path):
        os.remove(path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
    server.settimeout(8)

    def serve():
        try:
            conn, _ = server.accept()
        except OSError:
            server.close()
            return
        for line in lines:
            conn.sendall(line.encode() + b"\n")
            time.sleep(0.4)   # past the coalescing interval
        conn.close()
        server.close()
        os.remove(path)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return t


def on_event(name, data):
    received.append((name, data))
    if data == "7":
        got_second.set()


first = fake_instance(["workspace>>2", "garbage", "openwindow>>x,y,z,w"])
listener = he.HyprEventListener(on_event, path_finder=lambda: path if os.path.exists(path) else None)
listener.retry_interval = 0.1
listener.reconnect_delay = 0.1
listener.start()
first.join(timeout=8)
second = fake_instance(["workspace>>7"])   # "Hyprland restarts"
got_second.wait(timeout=8)
listener.stop()
second.join(timeout=8)

check("events from the first instance arrive", received[:2], [("workspace", "2"), ("openwindow", "x,y,z,w")])
check("the listener reconnects after a restart", ("workspace", "7") in received, True)


def boom(name, data):
    raise RuntimeError("bad handler")


# a handler that raises doesn't kill the listener
errors_seen = []
flaky = he.HyprEventListener(boom, path_finder=lambda: None)
check("a listener with no Hyprland starts and stops cleanly", (flaky.start(), flaky.stop()), (None, None))

if failures:
    print(f"{len(failures)} PROBLEM(S):")
    for f in failures:
        print("  ", f)
    sys.exit(1)
print("desktop event listener checks passed")
