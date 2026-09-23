#!/usr/bin/env python3
"""
toby_fold.py — the foldable-display lid animation.

    DESKTOP -> COMPRESS -> BEND -> FOLD TOWARD HINGE -> BLACK -> SUSPEND
    RESUME -> BLACK -> UNFOLD FROM HINGE -> EXPAND -> FLATTEN -> DESKTOP

A small daemon of its own, separate from Toby, so that Toby crashing can't
touch suspend and this crashing can't touch Toby. It runs as a systemd user
service (toby-fold.service) and restarts itself if it dies.

How it stays safe
-----------------
It never changes how the lid or suspend is configured. It asks systemd-logind
for a *delay* inhibitor lock on sleep: logind announces an upcoming suspend
with PrepareForSleep(true), waits for us to release the lock, and then
suspends. Three properties of that mechanism make it impossible for this
animation to stop the laptop from sleeping:

1. logind never waits longer than its own cap (InhibitDelayMaxSec, five
   seconds by default), whatever we do.
2. The lock is a file descriptor. If this process crashes, hangs and is
   killed, or the compositor goes away and takes it down, the descriptor
   closes and the lock is gone with it.
3. We release the lock ourselves on a hard deadline shortly after the fold
   should have finished, whether or not a single frame was ever drawn.

If a screenshot can't be taken, there is no animation at all: the lock is
released immediately and the laptop sleeps exactly as it always did.

The closing animation starts when logind decides to suspend, which for a lid
is when the lid switch trips — close to fully shut. So on most laptops only
the first part of the fold is visible before the screen is out of view. The
reopening unfold is fully visible, and ``toby sleep`` plays the fold in full
and then suspends, for when you want to watch it.

Testing
-------
FoldController holds all of the timing and state and knows nothing about GTK
or D-Bus; tests/test_fold.py drives it with a fake clock through rapid
close/open cycles, failed captures, late resumes and crashes mid-draw.
"""

import io
import json
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import toby_anim  # noqa: E402

# Never hold up suspend longer than this, even if logind would let us.
HARD_RELEASE_CAP_S = 1.8
# If we sit on a black screen this long *while awake*, the suspend we folded
# for never happened (it failed, or something inhibited it). Unfold. This is
# measured on the monotonic clock, which stops while the machine is asleep,
# so a real suspend of any length can never trip it.
FOLDED_WATCHDOG_S = 8.0
# Largest time step one frame may advance the animation by. A frame that
# arrives late (the machine was busy) slows the animation slightly rather
# than skipping half of it.
MAX_FRAME_STEP_S = 0.05


class FoldController:
    """All state and timing for the fold, independent of GTK and D-Bus.

    ``view`` must provide show(source), hide() and redraw(); ``inhibitor``
    must provide acquire() and release() and a ``held`` attribute;
    ``capture`` returns a prepared image source or None.
    """

    IDLE = "idle"
    FOLDING = "folding"
    FOLDED = "folded"
    UNFOLDING = "unfolding"
    FADING = "fading"        # shutdown: fade to black, no fold

    def __init__(self, view, inhibitor, capture, settings=None, clock=time.monotonic):
        self.view = view
        self.inhibitor = inhibitor
        self.capture = capture
        self.clock = clock
        self.reload_settings(settings)

        self.state = self.IDLE
        self.progress = 0.0          # 0 open .. 1 closed
        self.previous_progress = None
        self.source = None
        self._last_tick = None
        self._release_deadline = None
        self._folded_since = None
        self._previewing = False
        self.log = []                # (time, event) — for tests and `toby fold --status`

    def reload_settings(self, settings):
        self.settings = toby_anim.animation_settings(settings or {})

    # -- events ----------------------------------------------------------------
    def on_sleep(self, _preview=False):
        """logind is about to suspend and is waiting on our lock."""
        now = self.clock()
        self._note(now, "preview" if _preview else "sleep")
        if not _preview:
            # A real suspend always wins over a preview in progress: the
            # lock must be released on schedule, not held for a demo.
            self._previewing = False
        if not self.settings["fold_enabled"]:
            self._release("disabled")
            return

        if self.state == self.IDLE:
            try:
                self.source = self.capture()
            except Exception as e:  # capture must never be able to block sleep
                self._note(now, f"capture raised {e!r}")
                self.source = None
            if self.source is None:
                self._release("no screenshot")
                return
            try:
                self.view.show(self.source)
            except Exception as e:
                self._fail(f"show failed: {e!r}")
                return
            self.progress = 0.0
            self.previous_progress = None
        elif self.state == self.UNFOLDING:
            # Closed again while still opening: turn around from exactly
            # where we are, so rapid close/open never jumps.
            self._note(now, "reversing to fold")
        elif self.state in (self.FOLDING, self.FOLDED):
            pass  # already on our way down

        self.state = self.FOLDING if self.progress < 1.0 else self.FOLDED
        self._last_tick = now
        cap = min(HARD_RELEASE_CAP_S, self.settings["fold_close_duration"] + 0.35)
        self._release_deadline = now + cap
        if self.state == self.FOLDED:
            self._release("already folded")
            self._folded_since = now

    def on_resume(self):
        """The machine woke up (or a suspend was abandoned)."""
        now = self.clock()
        self._note(now, "resume")
        # take the lock again, ready for the next suspend
        try:
            self.inhibitor.acquire()
        except Exception as e:
            self._note(now, f"re-acquire failed: {e!r}")
        if self.state in (self.FOLDING, self.FOLDED, self.FADING):
            self.state = self.UNFOLDING
            self._last_tick = now
            self._release_deadline = None

    def on_shutdown(self):
        """Powering off: fade what's on screen to black."""
        now = self.clock()
        self._note(now, "shutdown")
        if not self.settings["shutdown_fade"]:
            self._release("shutdown fade disabled")
            return
        if self.state == self.IDLE:
            try:
                self.source = self.capture()
            except Exception:
                self.source = None
            if self.source is None:
                self._release("no screenshot")
                return
            try:
                self.view.show(self.source)
            except Exception as e:
                self._fail(f"show failed: {e!r}")
                return
        self.state = self.FADING
        self.progress = 0.0 if self.progress <= 0 else self.progress
        self._last_tick = now
        self._release_deadline = now + 0.9

    def preview(self):
        """Fold then unfold without suspending — for `toby fold --preview`.

        No suspend is pending, so the sleep lock must stay held throughout;
        releasing it here would leave the next real suspend unannounced.
        """
        if self.state != self.IDLE:
            return
        self._previewing = True
        self.on_sleep(_preview=True)

    # -- per-frame ------------------------------------------------------------
    def tick(self):
        """Advance one frame. Returns True while there is anything to draw."""
        now = self.clock()
        dt = 0.0 if self._last_tick is None else min(MAX_FRAME_STEP_S, max(0.0, now - self._last_tick))
        self._last_tick = now

        if self._release_deadline is not None and now >= self._release_deadline:
            self._release("deadline")

        self.previous_progress = self.progress
        if self.state == self.FOLDING:
            self.progress = min(1.0, self.progress + dt / self.settings["fold_close_duration"])
            if self.progress >= 1.0:
                self.state = self.FOLDED
                self._folded_since = now
                self._release("folded")
                if self._previewing:
                    self.state = self.UNFOLDING
        elif self.state == self.FADING:
            self.progress = min(1.0, self.progress + dt / 0.45)
            if self.progress >= 1.0:
                self._release("faded")
                if self._folded_since is None:
                    self._folded_since = now
                elif now - self._folded_since > FOLDED_WATCHDOG_S:
                    # still here long after power-off should have happened
                    self._note(now, "watchdog: shutdown never came, restoring")
                    self.state = self.UNFOLDING
        elif self.state == self.UNFOLDING:
            self.progress = max(0.0, self.progress - dt / self.settings["fold_open_duration"])
            if self.progress <= 0.0:
                self._finish()
                return False
        elif self.state == self.FOLDED:
            if self._folded_since is not None and now - self._folded_since > FOLDED_WATCHDOG_S:
                self._note(now, "watchdog: suspend never came, unfolding")
                self.state = self.UNFOLDING
        elif self.state == self.IDLE:
            return False

        try:
            self.view.redraw()
        except Exception as e:
            self._fail(f"draw failed: {e!r}")
            return False
        return True

    def check_deadlines(self):
        """The timing checks that must not depend on frames being drawn.

        Compositors stop sending frame callbacks when a display turns off or
        the screen locks — exactly what happens around a lid closing — so
        tick() can't be the only thing that releases the sleep lock or
        notices a suspend that never came. The overlay calls this from a
        plain clock timer as well. Returns True while it should keep being
        called.
        """
        now = self.clock()
        if self._release_deadline is not None and now >= self._release_deadline:
            self._release("deadline (no frames)")
        if self.state in (self.FOLDED, self.FADING) and self._folded_since is not None \
                and now - self._folded_since > FOLDED_WATCHDOG_S:
            self._note(now, "watchdog (no frames): unfolding")
            self.state = self.UNFOLDING
            self._last_tick = now
        return self.state != self.IDLE

    def draw_failed(self, error):
        """The view calls this if rendering a frame raised."""
        self._fail(f"draw failed: {error!r}")

    # -- internals ------------------------------------------------------------
    def _release(self, why):
        self._release_deadline = None
        if self._previewing:
            return  # nothing is waiting on us; keep the lock for the real thing
        if getattr(self.inhibitor, "held", False):
            self._note(self.clock(), f"release ({why})")
            try:
                self.inhibitor.release()
            except Exception as e:
                self._note(self.clock(), f"release raised {e!r}")

    def _finish(self):
        self.state = self.IDLE
        self.progress = 0.0
        self.previous_progress = None
        self.source = None            # drop the screenshot; it can be large
        self._folded_since = None
        self._previewing = False
        try:
            self.view.hide()
        except Exception:
            pass

    def _fail(self, why):
        """Anything went wrong: get out of the way completely."""
        self._note(self.clock(), why)
        self._release(why)
        self._finish()

    def _note(self, now, event):
        self.log.append((round(now, 3), event))
        del self.log[:-200]


# ---------------------------------------------------------------------------
# Everything below talks to the real system. Imported lazily so the
# controller above can be tested without GTK, D-Bus or a compositor.
# ---------------------------------------------------------------------------

def _hypr_monitors():
    try:
        out = subprocess.run(["hyprctl", "-j", "monitors"], capture_output=True,
                             text=True, timeout=1.0)
        return json.loads(out.stdout) if out.returncode == 0 else []
    except Exception:
        return []


def pick_monitor(monitors):
    """The laptop's own panel if there is one, else the focused monitor."""
    if not monitors:
        return None
    for m in monitors:
        if str(m.get("name", "")).startswith(("eDP", "LVDS", "DSI")):
            return m
    for m in monitors:
        if m.get("focused"):
            return m
    return monitors[0]


def capture_screen(monitor_name=None, timeout=0.8):
    """Screenshot one output with grim, returned as a prepared fold source.

    -l 0 turns PNG compression off: the image goes straight into memory and
    is thrown away after the animation, so compressing it is wasted time
    between logind's announcement and the first frame.
    """
    import cairo
    import fold_effect

    cmd = ["grim", "-l", "0"]
    if monitor_name:
        cmd += ["-o", monitor_name]
    cmd.append("-")
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout:
        return None
    surface = cairo.ImageSurface.create_from_png(io.BytesIO(out.stdout))
    return fold_effect.prepare_source(surface)


class LogindInhibitor:
    """A delay lock on sleep and shutdown, held as a file descriptor."""

    def __init__(self, bus):
        self._bus = bus
        self._fd = None

    @property
    def held(self):
        return self._fd is not None

    def acquire(self):
        if self._fd is not None:
            return
        from gi.repository import Gio, GLib

        result, fd_list = self._bus.call_with_unix_fd_list_sync(
            "org.freedesktop.login1", "/org/freedesktop/login1",
            "org.freedesktop.login1.Manager", "Inhibit",
            GLib.Variant("(ssss)", ("sleep:shutdown", "Little Toby",
                                    "Playing the lid fold animation", "delay")),
            GLib.VariantType.new("(h)"), Gio.DBusCallFlags.NONE, 2000, None, None)
        index = result.unpack()[0]
        self._fd = fd_list.get(index)

    def release(self):
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None


def _apply_layer_rules():
    """Ask Hyprland not to add its own fade to our overlay.

    Runtime only — nothing is written to your config. Both the older and
    newer layerrule spellings are tried; whichever this Hyprland doesn't
    understand is simply rejected. Without it the overlay may fade in over
    ~100 ms, which is barely visible because its first frame is identical to
    the desktop underneath.
    """
    for rule in ("noanim, toby-fold", "no_anim on, match:namespace toby-fold"):
        try:
            subprocess.run(["hyprctl", "keyword", "layerrule", rule],
                           capture_output=True, timeout=1.0)
        except Exception:
            pass


def make_overlay_class():
    """Build the overlay window class. Deferred so importing this module
    never needs GTK — only running the daemon (or its smoke test) does."""
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("GtkLayerShell", "0.1")
    # Needed before a cairo.Region can be handed to GDK (for the empty input
    # region that lets clicks pass through). Without it that call raises,
    # and the fold quietly never shows.
    gi.require_foreign("cairo")
    from gi.repository import Gdk, GLib, Gtk, GtkLayerShell

    import cairo
    import fold_effect

    class FoldOverlay:
        """Full-screen layer that shows the folding desktop.

        Input passes straight through it at all times, so even a frozen
        frame could never trap a click. It only exists on screen while an
        animation is running, and frames are driven by the compositor's
        frame clock only while it does — nothing is drawn while idle.
        """

        def __init__(self):
            self.window = Gtk.Window()
            GtkLayerShell.init_for_window(self.window)
            GtkLayerShell.set_layer(self.window, GtkLayerShell.Layer.OVERLAY)
            GtkLayerShell.set_namespace(self.window, "toby-fold")
            for edge in (GtkLayerShell.Edge.TOP, GtkLayerShell.Edge.BOTTOM,
                         GtkLayerShell.Edge.LEFT, GtkLayerShell.Edge.RIGHT):
                GtkLayerShell.set_anchor(self.window, edge, True)
            GtkLayerShell.set_exclusive_zone(self.window, -1)
            GtkLayerShell.set_keyboard_mode(self.window, GtkLayerShell.KeyboardMode.NONE)
            self.window.set_decorated(False)
            self.area = Gtk.DrawingArea()
            self.area.connect("draw", self._on_draw)
            self.window.add(self.area)
            self._tick_id = None
            self.controller = None

        def show(self, source):
            monitor = self._target_gdk_monitor()
            if monitor is not None:
                GtkLayerShell.set_monitor(self.window, monitor)
            self.window.show_all()
            # an empty input region: every click goes to what's underneath
            gdk_window = self.window.get_window()
            if gdk_window is not None:
                gdk_window.input_shape_combine_region(cairo.Region(), 0, 0)
            if self._tick_id is None:
                self._tick_id = self.area.add_tick_callback(self._on_tick)
            if getattr(self, "_watchdog_id", None) is None:
                self._watchdog_id = GLib.timeout_add(100, self._on_watchdog)

        def _on_watchdog(self):
            keep = self.controller.check_deadlines()
            if self.controller.state == self.controller.UNFOLDING and self._tick_id is None:
                self._tick_id = self.area.add_tick_callback(self._on_tick)
            if not keep:
                self._watchdog_id = None
            return keep

        def hide(self):
            if self._tick_id is not None:
                self.area.remove_tick_callback(self._tick_id)
                self._tick_id = None
            self.window.hide()

        def redraw(self):
            self.area.queue_draw()

        def _on_tick(self, _widget, _frame_clock):
            keep_going = self.controller.tick()
            if not keep_going:
                self._tick_id = None
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE

        def _on_draw(self, widget, cr):
            c = self.controller
            if c is None or c.source is None:
                cr.set_source_rgb(0, 0, 0)
                cr.paint()
                return False
            try:
                w, h = widget.get_allocated_width(), widget.get_allocated_height()
                params = fold_effect.fold_params(c.settings)
                if c.state == c.FADING:
                    cr.set_source_surface(c.source, 0, 0)
                    cr.scale(w / c.source.get_width(), h / c.source.get_height())
                    cr.paint()
                    cr.identity_matrix()
                    cr.set_source_rgba(0, 0, 0, toby_anim.ease_in_out_sine(c.progress))
                    cr.paint()
                else:
                    fold_effect.render_fold(cr, w, h, c.source, c.progress, params,
                                            c.previous_progress)
            except Exception as e:
                GLib.idle_add(lambda: c.draw_failed(e) or False)
            return False

        def _target_gdk_monitor(self):
            chosen = pick_monitor(_hypr_monitors())
            display = Gdk.Display.get_default()
            if chosen is None or display is None:
                return None
            for i in range(display.get_n_monitors()):
                monitor = display.get_monitor(i)
                geo = monitor.get_geometry()
                if geo.x == chosen.get("x") and geo.y == chosen.get("y"):
                    return monitor
            return None

    return FoldOverlay


def run_daemon(preview=False, sleep_after=False):
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("GtkLayerShell", "0.1")
    from gi.repository import Gdk, Gio, GLib, Gtk, GtkLayerShell

    import toby_settings

    settings = toby_settings.load()
    FoldOverlay = make_overlay_class()

    def capture():
        chosen = pick_monitor(_hypr_monitors())
        return capture_screen(chosen.get("name") if chosen else None)

    system_bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    inhibitor = LogindInhibitor(system_bus)
    overlay = FoldOverlay()
    controller = FoldController(overlay, inhibitor, capture, settings)
    overlay.controller = controller

    try:
        inhibitor.acquire()
    except Exception as e:
        print(f"toby-fold: couldn't take a sleep delay lock ({e}); "
              "the fold will still try to play, but can't delay suspend for it", flush=True)

    def on_signal(_conn, _sender, _path, _iface, name, params, _data=None):
        starting = params.unpack()[0]
        if name == "PrepareForSleep":
            GLib.idle_add(lambda: (controller.on_sleep() if starting else controller.on_resume()) and False)
        elif name == "PrepareForShutdown":
            # false means a shutdown was cancelled: put the desktop back
            GLib.idle_add(lambda: (controller.on_shutdown() if starting else controller.on_resume()) and False)

    system_bus.signal_subscribe("org.freedesktop.login1", "org.freedesktop.login1.Manager",
                                None, "/org/freedesktop/login1", None,
                                Gio.DBusSignalFlags.NONE, on_signal, None)

    # `systemctl --user reload toby-fold` or `toby fold --reload` re-reads settings
    def on_hup(*_a):
        controller.reload_settings(toby_settings.load())
        return True

    # `toby fold --preview` sends SIGUSR2: fold and unfold without sleeping
    def on_usr2(*_a):
        controller.reload_settings(toby_settings.load())
        controller.preview()
        return True

    # `toby sleep` sends SIGUSR1: play the whole fold with the lid still
    # open, then suspend. logind's own announcement then finds us already
    # folded and gets the lock back straight away.
    def fold_then_sleep(*_a):
        controller.reload_settings(toby_settings.load())
        controller.on_sleep()
        delay_ms = int(controller.settings["fold_close_duration"] * 1000) + 120
        GLib.timeout_add(delay_ms, lambda: subprocess.Popen(["systemctl", "suspend"]) and False)
        return True

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, fold_then_sleep)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGHUP, on_hup)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR2, on_usr2)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda *_: Gtk.main_quit() or False)

    _apply_layer_rules()

    if preview:
        GLib.timeout_add(300, lambda: controller.preview() or False)
    if sleep_after:
        GLib.timeout_add(200, lambda: fold_then_sleep() and False)

    Gtk.main()
    inhibitor.release()


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="Little Toby lid fold animation")
    parser.add_argument("--preview", action="store_true",
                        help="play the fold and unfold once, without suspending")
    parser.add_argument("--sleep", action="store_true",
                        help="play the full fold, then suspend")
    args = parser.parse_args(argv)
    run_daemon(preview=args.preview, sleep_after=args.sleep)


if __name__ == "__main__":
    main()
