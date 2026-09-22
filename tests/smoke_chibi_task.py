"""Run a real multi-step task through the app with the chibi on.

The task runner executes on a background thread while the GTK main loop
runs here, exactly as it does in the app; only the effects on the real
desktop (ydotool, xdg-open) are replaced with recorders. Checks that the
chibi comes out, every step ends up done and struck through, the pointer
waits for the chibi's hand, and everything is put away afterwards. Saves a
frame from the middle of the task.
"""
import os
import sys
import tempfile
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _layer_shell_stub  # noqa: E402

_layer_shell_stub.install()

from gi.repository import GLib  # noqa: E402

import cairo  # noqa: E402

import chibi  # noqa: E402
import linux_agent_apple as app  # noqa: E402

errors = []
complaints = []
for domain in ("Gtk", "Gdk", "GLib", "GLib-GObject"):
    GLib.log_set_handler(domain, GLib.LogLevelFlags.LEVEL_CRITICAL | GLib.LogLevelFlags.LEVEL_WARNING,
                         lambda d, lvl, msg, _u=None: complaints.append(f"{d}: {msg}"), None)

calls = []
chibi_at_move = []


def pump(seconds, until=None):
    end = time.monotonic() + seconds
    ctx = GLib.MainContext.default()
    while time.monotonic() < end:
        ctx.iteration(False)
        if until and until():
            return True
        time.sleep(0.004)
    return False


try:
    win = app.AssistantWindow(app.RingFlash())
    win.show_panel()
    pump(0.5)

    # Replace the effects on the real desktop with recorders.
    def fake_move(a):
        # where is the chibi's hand when the pointer is told to move?
        chibi_at_move.append(win.chibi_director.hand_position())
        calls.append(("move", a["x"], a["y"]))
        return "moved"

    app.DISPATCH["open_url"] = lambda a: calls.append(("open", a["url"])) or "opened"
    app.DISPATCH["move_mouse"] = fake_move
    app.DISPATCH["click_mouse"] = lambda a: calls.append(("click",)) or "clicked"
    app.DISPATCH["type_text"] = lambda a: calls.append(("type", a["text"])) or "typed"

    actions = [
        {"tool": "open_url", "url": "https://www.khanacademy.org"},
        {"tool": "move_mouse", "x": 0.3, "y": 0.25},
        {"tool": "click_mouse", "button": "left"},
        {"tool": "type_text", "text": "quadratics"},
    ]
    win.task_label_text = "search Khan Academy for quadratics"
    win.task_steps = [("Thinking", "current")]

    outcome = {}
    runner = threading.Thread(target=lambda: outcome.setdefault("n", win._execute_actions(actions)))
    runner.start()

    frame_saved = [False]
    workdir = tempfile.mkdtemp()

    def save_frame_when_busy():
        d = win.chibi_director
        if not frame_saved[0] and d.visible and d.body >= 1.0 and len(calls) >= 2:
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1280, 1024)
            c = cairo.Context(surface)
            c.set_source_rgb(0.16, 0.17, 0.3)   # a stand-in desktop behind the stage
            c.paint()
            win.chibi_stage.on_draw(win.chibi_stage.area, c)
            card_x = win.chibi_stage._card_rect()[0]
            if card_x < 0 or card_x + chibi.CARD_WIDTH > 1280:
                errors.append(f"the checklist card is off screen (x={card_x})")
            if d.bubble != app.describe_action(actions[min(len(calls), 3)]) and d.bubble not in (
                    app.describe_action(a) for a in actions):
                errors.append(f"the bubble doesn't describe a current step: {d.bubble!r}")
            surface.write_to_png(os.path.join(workdir, "chibi_mid_task.png"))
            frame_saved[0] = True
        return not runner.is_alive()

    if not win.chibi_director.visible:
        pump(1.0, until=lambda: win.chibi_director.visible)
    if not win.chibi_director.visible:
        errors.append("the chibi never came out for a task with on-screen steps")

    pump(15, until=save_frame_when_busy)
    runner.join(timeout=1)
    pump(0.2)   # let the last status updates the runner queued reach the main loop
    if runner.is_alive():
        errors.append("the task runner never finished (a wait on the chibi hung)")

    check_order = [c[0] for c in calls]
    if check_order != ["open", "move", "click", "type"]:
        errors.append(f"steps ran out of order or went missing: {check_order}")
    if outcome.get("n") != 4:
        errors.append(f"expected 4 successful steps, got {outcome.get('n')}")

    screen = win.get_screen()
    if chibi_at_move:
        hx, hy = chibi_at_move[0]
        want = (0.3 * screen.get_width(), 0.25 * screen.get_height())
        if abs(hx - want[0]) > 2 or abs(hy - want[1]) > 2:
            errors.append(f"the pointer moved before the chibi's hand got there: hand {chibi_at_move[0]}, target {want}")

    visible_steps = win.chibi_director.steps
    if [s for _l, s in visible_steps] != ["done"] * 4:
        errors.append(f"the checklist should show all four steps done: {visible_steps}")
    labels = [l for l, _s in visible_steps]
    if labels[0] != "Open www.khanacademy.org" or labels[3] != 'Type "quadratics"':
        errors.append(f"step labels aren't human-readable: {labels}")

    # the reply arrives; the chibi says it, cheers, and goes home
    win.finish_response("search Khan Academy", {"reply": "Done, it's searching now.", "mood": "happy"}, 4)
    if win.chibi_director.bubble != "Done, it's searching now.":
        errors.append("the chibi didn't say the reply")
    gone = pump(8, until=lambda: not win.chibi_director.visible)
    if not gone:
        errors.append("the chibi never went home after the task")
    pump(0.6)
    if win.chibi_stage.get_visible():
        errors.append("the stage window was left up after the chibi left")
    if win.face.state == app.State.SLEEPING and win.get_visible():
        errors.append("the face never came back into the pill")
    print(f"a mid-task frame is at {workdir}/chibi_mid_task.png" if frame_saved[0]
          else "no mid-task frame captured")
except Exception:
    errors.append(traceback.format_exc())

errors += ["GTK complained: " + c for c in complaints]
if errors:
    print(f"{len(errors)} PROBLEM(S):")
    for e in errors:
        print("  ", e)
    sys.exit(1)
print("chibi task smoke test passed")
