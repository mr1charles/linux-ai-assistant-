"""Build the real fold overlay window under Xvfb and run a full fold and
unfold through it, drawing every frame the way the compositor would ask for
them. Catches mistakes in the GTK half of toby_fold.py, which the pure state
machine tests in test_fold.py can't reach, and saves one mid-fold frame so it
can be looked at.
"""
import os
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _layer_shell_stub  # noqa: E402

REPO_ROOT = _layer_shell_stub.install()

from gi.repository import GLib  # noqa: E402

import cairo  # noqa: E402

import fold_effect  # noqa: E402
import make_fake_desktop  # noqa: E402
import toby_fold  # noqa: E402

errors = []
complaints = []
for domain in ("Gtk", "Gdk", "GLib", "GLib-GObject", "cairo"):
    GLib.log_set_handler(domain, GLib.LogLevelFlags.LEVEL_CRITICAL | GLib.LogLevelFlags.LEVEL_WARNING,
                         lambda d, lvl, msg, _u=None: complaints.append(f"{d}: {msg}"), None)

workdir = tempfile.mkdtemp()
desk = os.path.join(workdir, "desk.png")
make_fake_desktop.make(desk, 1366, 768)
source = fold_effect.prepare_source(desk)


class Widget:
    def get_allocated_width(self):
        return 1366

    def get_allocated_height(self):
        return 768


class Lock:
    held = True

    def acquire(self):
        self.held = True

    def release(self):
        self.held = False


try:
    FoldOverlay = toby_fold.make_overlay_class()
    overlay = FoldOverlay()
    now = [0.0]
    controller = toby_fold.FoldController(overlay, Lock(), lambda: source, {},
                                          clock=lambda: now[0])
    overlay.controller = controller
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1366, 768)
    saved = False

    controller.on_sleep()
    if not overlay.window.get_visible():
        errors.append("the overlay window did not appear when the fold began")
    frames = 0
    for _ in range(200):
        now[0] += 1 / 60
        running = controller.tick()
        cr = cairo.Context(surface)
        overlay._on_draw(Widget(), cr)
        frames += 1
        if not saved and controller.progress > 0.5:
            surface.write_to_png(os.path.join(workdir, "mid_fold.png"))
            saved = True
        if controller.state == controller.FOLDED:
            break
    controller.on_resume()
    for _ in range(200):
        now[0] += 1 / 60
        if not controller.tick():
            break
        overlay._on_draw(Widget(), cairo.Context(surface))
    if overlay.window.get_visible():
        errors.append("the overlay window was still up after the unfold finished")
    if controller.state != controller.IDLE:
        errors.append(f"expected idle after the unfold, got {controller.state}")
    print(f"drew {frames} closing frames; a mid-fold frame is at {workdir}/mid_fold.png")
except Exception:
    errors.append(traceback.format_exc())

errors += ["GTK complained: " + c for c in complaints]
if errors:
    print(f"{len(errors)} PROBLEM(S):")
    for e in errors:
        print("  ", e)
    sys.exit(1)
print("fold overlay smoke test passed")
