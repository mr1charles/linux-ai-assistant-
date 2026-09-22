"""
hypr_events.py — hear what the desktop is doing, so Toby can react to it.

Hyprland publishes a line-per-event stream on a UNIX socket (".socket2.sock"):
``workspace>>3``, ``openwindow>>...``, ``activewindow>>firefox,Title`` and so
on. Toby listens to a handful of them to glance toward a workspace you just
switched to or look up when a window opens — small reactions that make the
character feel present without ever interrupting anything.

Robustness is the whole design:

- Listening is read-only; nothing is ever sent to Hyprland on this socket.
- It runs on its own daemon thread with a blocking read, so it costs nothing
  while the desktop is quiet.
- If Hyprland restarts, the socket closes; the listener notices, waits, and
  reconnects to the new instance with a backoff, forever, without the rest
  of Toby noticing anything.
- Events are coalesced: a burst of fifty window events during a workspace
  swap produces one reaction, not fifty.
"""

import os
import socket
import threading
import time

REACT_EVENTS = {"workspace", "workspacev2", "openwindow", "closewindow",
                "activewindow", "fullscreen", "urgent"}


def socket_path(env=None):
    """The event socket of the running Hyprland.

    Prefer the instance named in our environment, but if that one is gone
    — Hyprland restarted, and the new instance has a new signature our
    environment doesn't know about — fall back to the most recently started
    instance that has a live socket. Without that, a listener outliving a
    compositor restart would wait on a dead socket forever.
    """
    env = env if env is not None else os.environ
    runtime = env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    bases = [os.path.join(runtime, "hypr"), "/tmp/hypr"]
    signature = env.get("HYPRLAND_INSTANCE_SIGNATURE")
    if signature:
        for base in bases:
            path = os.path.join(base, signature, ".socket2.sock")
            if os.path.exists(path):
                return path
    newest, newest_time = None, -1.0
    for base in bases:
        try:
            entries = os.listdir(base)
        except OSError:
            continue
        for entry in entries:
            path = os.path.join(base, entry, ".socket2.sock")
            try:
                mtime = os.stat(path).st_mtime
            except OSError:
                continue
            if mtime > newest_time:
                newest, newest_time = path, mtime
    if newest:
        return newest
    if signature:
        return os.path.join(bases[0], signature, ".socket2.sock")
    return None


def parse_line(line):
    """``"workspace>>3"`` -> ``("workspace", "3")``; junk -> None."""
    if ">>" not in line:
        return None
    name, _, data = line.partition(">>")
    name = name.strip()
    if not name:
        return None
    return name, data.strip()


class Coalescer:
    """Collapse bursts: at most one reaction per kind per interval."""

    def __init__(self, interval=0.35, clock=time.monotonic):
        self.interval = interval
        self.clock = clock
        self._last = {}

    def allow(self, kind):
        now = self.clock()
        if now - self._last.get(kind, -1e9) < self.interval:
            return False
        self._last[kind] = now
        return True


class HyprEventListener:
    """Calls on_event(name, data) for the events Toby cares about.

    on_event runs on the listener thread; the caller marshals onto GTK.
    """

    def __init__(self, on_event, path_finder=socket_path, connect=None):
        self.on_event = on_event
        self._path_finder = path_finder
        self._connect = connect or self._connect_unix
        self._stop = threading.Event()
        self._thread = None
        self._coalescer = Coalescer()
        self.connected = False
        self.retry_interval = 5.0    # how often to look for a Hyprland that isn't there
        self.reconnect_delay = 1.0   # pause after an instance goes away

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="hypr-events")
        self._thread.start()

    def stop(self):
        self._stop.set()

    @staticmethod
    def _connect_unix(path):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(path)
        sock.settimeout(1.0)   # short, so stop() is noticed promptly
        return sock

    def _run(self):
        backoff = 0.5
        while not self._stop.is_set():
            path = self._path_finder()
            if not path:
                # no Hyprland running right now; look again shortly
                self._stop.wait(self.retry_interval)
                continue
            try:
                sock = self._connect(path)
            except OSError:
                self._stop.wait(backoff)
                backoff = min(10.0, backoff * 2)
                continue
            backoff = 0.5
            self.connected = True
            try:
                self._read(sock)
            finally:
                self.connected = False
                try:
                    sock.close()
                except OSError:
                    pass
            # Hyprland went away (restart, crash, logout). Give it a moment.
            self._stop.wait(self.reconnect_delay)

    def _read(self, sock):
        buffer = b""
        while not self._stop.is_set():
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if not chunk:
                return   # the other end closed
            buffer += chunk
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                parsed = parse_line(raw.decode("utf-8", "replace"))
                if not parsed:
                    continue
                name, data = parsed
                if name in REACT_EVENTS and self._coalescer.allow(name):
                    try:
                        self.on_event(name, data)
                    except Exception as e:  # a bad reaction must not kill the listener
                        print("HYPR EVENT HANDLER ERROR:", e, flush=True)
            if len(buffer) > 65536:
                buffer = b""   # a line that never ends is not a line worth keeping
