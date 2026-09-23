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
app.SETTINGS["startup_greeting"] = False

errors = []
complaints = []
for domain in ("Gtk", "Gdk", "GLib", "GLib-GObject"):
    GLib.log_set_handler(domain, GLib.LogLevelFlags.LEVEL_CRITICAL | GLib.LogLevelFlags.LEVEL_WARNING,
                         lambda d, lvl, msg, _u=None: complaints.append(f"{d}: {msg}"), None)

calls = []


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
    # the phone bridge on, on any free port
    app.SETTINGS["remote_enabled"] = True
    app.SETTINGS["remote_port"] = 0
    win = app.AssistantWindow(app.RingFlash())
    win.show_panel()
    pump(0.5)

    # Replace only the lowest-level effects on the real desktop (ydotool,
    # hyprctl, xdg-open) with recorders. The real ScreenControl runs: its
    # consent gate, its glide on the main loop, its waits on the task thread.
    import screen_control as sc_mod
    d = win.chibi_director
    pointer_log = []     # (x, y, hand x, hand y, holding) for every pointer move
    seen = {}

    def fake_move_pointer(px, py, timeout=1.5):
        hx, hy = d.hand_position()
        pointer_log.append((px, py, hx, hy, d.holding))
        return "ok"

    def fake_button(button, timeout=2):
        seen["click"] = {"at": app.screen_control.pointer_pixels(), "ripples": len(d.ripples),
                         "pose": d.pose}
        calls.append(("click",))
        return f"Clicked {button}"

    def fake_ydotool(args, timeout):
        if args[0] == "type":
            seen["type"] = {"keyboard": (d.keyboard_state() or {}).get("amount", 0.0),
                            "holding": d.holding}
            calls.append(("type", args[1]))
            time.sleep(0.3)   # typing takes a moment; the bubble types along
        elif args[0] == "key":
            caps = d.keycaps_state()
            seen["key"] = {"caps": caps[0] if caps else None, "all_down": bool(caps and all(caps[1]))}
            calls.append(("key", " ".join(args[1:])))
        return "ok"

    sc_mod.move_pointer = fake_move_pointer
    sc_mod.press_button = fake_button
    sc_mod._run_ydotool = fake_ydotool
    sc_mod.read_cursor = lambda timeout=0.5: (900.0, 700.0)   # where the user left it
    app.DISPATCH["open_url"] = lambda a: calls.append(("open", a["url"])) or "opened"
    original_move = app.DISPATCH["move_mouse"]
    app.DISPATCH["move_mouse"] = lambda a: calls.append(("move", a["x"], a["y"])) or original_move(a)
    app.screen_control.deny()   # nobody has said yes yet

    actions = [
        {"tool": "open_url", "url": "https://www.khanacademy.org"},
        {"tool": "move_mouse", "x": 0.3, "y": 0.25},
        {"tool": "click_mouse", "button": "left"},
        {"tool": "type_text", "text": "quadratics"},
        {"tool": "key_press", "keys": "ctrl+enter"},
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
        if not frame_saved[0] and d.visible and d.body >= 1.0 and d.holding:
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1280, 1024)
            c = cairo.Context(surface)
            c.set_source_rgb(0.16, 0.17, 0.3)   # a stand-in desktop behind the stage
            c.paint()
            win.chibi_stage.on_draw(win.chibi_stage.area, c)
            card_x = win.chibi_stage._card_rect()[0]
            if card_x < 0 or card_x + chibi.CARD_WIDTH > 1280:
                errors.append(f"the checklist card is off screen (x={card_x})")
            if d.bubble not in (app.describe_action(a) for a in actions):
                errors.append(f"the bubble doesn't describe a current step: {d.bubble!r}")
            surface.write_to_png(os.path.join(workdir, "chibi_mid_task.png"))
            frame_saved[0] = True
        return not runner.is_alive()

    if not win.chibi_director.visible:
        pump(1.0, until=lambda: win.chibi_director.visible)
    if not win.chibi_director.visible:
        errors.append("the chibi never came out for a task with on-screen steps")

    # Toby asks before touching the mouse, and doesn't grab it first
    asked = pump(6, until=lambda: win.confirm_row.get_visible())
    if not asked:
        errors.append("Toby never asked before using the mouse")
    else:
        if d.holding or pointer_log:
            errors.append("the chibi took hold of the pointer before being allowed to")
        if "mouse" not in d.bubble_text():
            errors.append(f"the chibi didn't ask in its bubble: {d.bubble_text()!r}")
        pump(0.3)
        win.on_confirm_yes()

    typing_bubbles = []

    def watch():
        text = d.bubble_text()
        if d._typing and text.strip("| "):
            typing_bubbles.append(text.rstrip("| "))
        return save_frame_when_busy()

    pump(20, until=watch)
    runner.join(timeout=1)
    pump(0.2)   # let the last status updates the runner queued reach the main loop
    if runner.is_alive():
        errors.append("the task runner never finished (a wait on the chibi hung)")

    check_order = [c[0] for c in calls]
    if check_order != ["open", "move", "click", "type", "key"]:
        errors.append(f"steps ran out of order or went missing: {check_order}")
    if outcome.get("n") != 5:
        errors.append(f"expected 5 successful steps, got {outcome.get('n')}")

    # the pointer is carried: it only moves while Toby holds it, and his
    # hand stays on it the whole way
    screen = win.get_screen()
    want = (int(0.3 * screen.get_width()), int(0.25 * screen.get_height()))
    if not pointer_log:
        errors.append("the pointer never moved")
    else:
        if not all(entry[4] for entry in pointer_log):
            errors.append("the pointer moved while the chibi wasn't holding it")
        start = pointer_log[0]
        # (the stale remembered position was the middle of the screen)
        if abs(start[0] - 900) > 60 or abs(start[1] - 700) > 60:
            errors.append(f"the carry didn't start from where the pointer was: {start[:2]}")
        if tuple(pointer_log[-1][:2]) != want:
            errors.append(f"the pointer didn't end on the target: {pointer_log[-1][:2]} vs {want}")
        steps_px = [((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
                    for a, b in zip(pointer_log, pointer_log[1:])]
        if len(pointer_log) < 8:
            errors.append(f"the carry was a jump, not a glide ({len(pointer_log)} moves)")
        elif max(steps_px) > 120:
            errors.append(f"the pointer jumped {max(steps_px):.0f}px in one frame")
        else:
            # The hand is drawn on frames, the pointer moves on its own
            # timer, so the hand can trail by one frame's worth of travel
            # and never more.
            gaps = [((px - hx) ** 2 + (py - hy) ** 2) ** 0.5 for px, py, hx, hy, _h in pointer_log]
            if max(gaps) > 2 * max(steps_px) + 6:
                errors.append(f"the hand fell {max(gaps):.0f}px behind the pointer during the carry "
                              f"(largest pointer step {max(steps_px):.0f}px)")

    click = seen.get("click", {})
    if not click:
        errors.append("the click never happened")
    else:
        if click["at"] is None or tuple(int(v) for v in click["at"]) != want:
            errors.append(f"the click landed before the pointer arrived: {click['at']}")
        if not click["ripples"]:
            errors.append("the click had no visible ring")
    typed = seen.get("type", {})
    if typed.get("keyboard", 0) < 0.5:
        errors.append("the keyboard wasn't out when typing began")
    if typed.get("holding"):
        errors.append("the chibi was still holding the pointer while typing")
    if not typing_bubbles or not all("quadratics".startswith(t) for t in typing_bubbles):
        errors.append(f"the bubble didn't type along: {typing_bubbles[:5]}")
    elif len(set(typing_bubbles)) < 2:
        errors.append("the typed text appeared all at once instead of letter by letter")
    key = seen.get("key", {})
    if key.get("caps") != ["Ctrl", "Enter"] or not key.get("all_down"):
        errors.append(f"the key combination wasn't shown pressed as it landed: {key}")

    visible_steps = win.chibi_director.steps
    if [s for _l, s in visible_steps] != ["done"] * 5:
        errors.append(f"the checklist should show all five steps done: {visible_steps}")
    labels = [l for l, _s in visible_steps]
    if labels[0] != "Open www.khanacademy.org" or labels[3] != 'Type "quadratics"':
        errors.append(f"step labels aren't human-readable: {labels}")
    pump(0.6)
    if d.keyboard_state() is not None:
        errors.append("the keyboard wasn't put away after typing")

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
    # -- the fast path: "open youtube" never touches the model ---------------
    calls.clear()
    app.think = lambda *a, **k: errors.append("the model was called for 'open youtube'") or {
        "actions": [], "reply": "", "mood": "neutral"}
    win.task_label_text = "open youtube"
    win.task_steps = [("Thinking", "current")]
    started = time.monotonic()
    fast = threading.Thread(target=win.process, args=("hey toby, open youtube please",))
    fast.start()
    pump(10, until=lambda: not fast.is_alive())
    pump(0.3)
    if calls != [("open", "https://www.youtube.com")]:
        errors.append(f"the fast path didn't open YouTube: {calls}")
    if win.answer.get_text() != "Opening YouTube.":
        errors.append(f"the fast path reply didn't show: {win.answer.get_text()!r}")
    pump(8, until=lambda: not win.chibi_director.visible)

    # -- a request from the phone, end to end over HTTP ----------------------
    import json as _json
    import urllib.request as _url
    calls.clear()
    if win.remote is None:
        errors.append("the phone bridge didn't start with remote_enabled on")
    else:
        rhost, rport = win.remote.address
        token = win.remote.token

        def phone(path, body=None):
            req = _url.Request(f"http://{rhost}:{rport}{path}",
                               data=None if body is None else _json.dumps(body).encode(),
                               method="GET" if body is None else "POST")
            req.add_header("Authorization", "Bearer " + token)
            req.add_header("Content-Type", "application/json")
            with _url.urlopen(req, timeout=5) as r:
                return _json.loads(r.read())

        replies = {}
        t = threading.Thread(target=lambda: replies.setdefault("ask", phone("/api/ask", {"text": "open youtube and tiktok"})))
        t.start()
        pump(3, until=lambda: not t.is_alive())
        if not replies.get("ask", {}).get("ok"):
            errors.append(f"the phone's request was refused: {replies}")
        pump(12, until=lambda: not win._busy and len(calls) >= 2)
        pump(0.5)
        if calls != [("open", "https://www.youtube.com"), ("open", "https://www.tiktok.com")]:
            errors.append(f"the phone's request didn't run on the laptop: {calls}")
        holder = {}
        t = threading.Thread(target=lambda: holder.setdefault("s", phone("/api/state")))
        t.start()
        pump(3, until=lambda: not t.is_alive())
        state = holder.get("s", {})
        if state.get("reply") != "Opening YouTube and TikTok.":
            errors.append(f"the phone didn't get the reply: {state.get('reply')!r}")
        if [st["status"] for st in state.get("steps", [])] != ["done", "done"]:
            errors.append(f"the phone didn't see both steps done: {state.get('steps')}")
        if state.get("busy"):
            errors.append("the phone still thinks Toby is busy")
        pump(8, until=lambda: not win.chibi_director.visible)
        win.remote.stop()

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
