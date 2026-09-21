"""Headless smoke test for linux_agent_apple.py.

Little Toby's UI only truly runs on a Wayland compositor, because every
window it opens is a layer-shell surface. That makes it awkward to check
anything automatically. This test gets most of the way there anyway:
GtkLayerShell is replaced with a no-op stub, so the entire widget tree
(main pill, sidebar window, Dynamic Island, study windows, the whole
Settings page) is really constructed against real GTK under Xvfb, every
custom Cairo widget is really drawn, and the parsing helpers are checked
against known inputs.

What it catches: bad GTK API usage, missing attributes, construction-order
mistakes, crashes inside a draw handler, and regressions in the JSON reply
parsing. What it cannot catch: anchoring, margins, input regions, and
anything else that only means something to a live compositor.

Run it with:  ./tests/run.sh
"""
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _layer_shell_stub  # noqa: E402

REPO_ROOT = _layer_shell_stub.install()

from gi.repository import Gtk  # noqa: E402,F401

import linux_agent_apple as app
import toby_settings
import knowledge

print("import OK")

errors = []
win = None
try:
    ring = app.RingFlash()
    win = app.AssistantWindow(ring)
    print("AssistantWindow constructed OK")
except Exception:
    errors.append("construction: " + traceback.format_exc())

if win is not None:
    # exercise the handlers that don't need a compositor or network
    checks = [
        ("refresh_dashboard", lambda: win.refresh_dashboard()),
        ("switch_nav(plan)", lambda: win.switch_nav("plan")),
        ("switch_nav(settings)", lambda: win.switch_nav("settings")),
        ("switch_nav(memories)", lambda: win.switch_nav("memories")),
        ("refresh_memory_graph", lambda: win.refresh_memory_graph()),
        ("every memory graph layout", lambda: [
            win.on_memory_layout_clicked(k) for k, _ in toby_settings.MEMORY_GRAPH_LAYOUTS]),
        ("refresh_study_plan_grid", lambda: win.refresh_study_plan_grid()),
        ("refresh_camera_gesture_list", lambda: win.refresh_camera_gesture_list()),
        ("refresh_study_helper_stats", lambda: win.refresh_study_helper_stats()),
        ("apply_accent_color", lambda: win.apply_accent_color("#5a8cff")),
        ("island.set_status", lambda: win.island.set_status("test")),
        ("island_expanded.set_task", lambda: win.island_expanded.set_task(
            "task", [("Thinking", "done"), ("open_url", "current"), ("close_tab", "pending")])),
        ("face.tick", lambda: [win.face.tick(m) for m in ("happy", "neutral", "concerned", "excited")]),
        ("_apply_voice_state", lambda: [win._apply_voice_state(s) for s in
            ("listening_wake", "listening_command", "speaking", "idle", "error: x")]),
        ("_cycle_nav_section", lambda: win._cycle_nav_section()),
        ("_append_history_row", lambda: win._append_history_row("hi", "hello")),
        ("_extend_task_steps", lambda: win._extend_task_steps([{"tool": "open_url"}])),
        ("_set_task_step_status", lambda: win._set_task_step_status(0, "done")),
        ("waveform.push_level", lambda: [win.waveform.push_level(i / 10) for i in range(12)]),
        ("camera_overlay.set_hands", lambda: win.camera_overlay.set_hands(
            [[(0.5, 0.5)] * 21])),
        ("study_helper.set_stats", lambda: win.study_helper.set_stats("0 summaries")),
        ("topic_detail.open_topic (assessed)", lambda: win.topic_detail.open_topic(
            {"id": "t1", "subject": "ELA", "topic": "Text analysis essays",
             "proficiency": 72, "quiz_count": 3, "last_assessed": None, "sources": []})),
        ("topic_detail.open_topic (unassessed)", lambda: win.topic_detail.open_topic(
            {"id": "t2", "subject": "Math", "topic": "Quadratics",
             "proficiency": None, "quiz_count": 0, "last_assessed": None, "sources": []})),
        ("study_review.open_review", lambda: win.study_review.open_review(
            "flashcards", [{"front": "a", "back": "b"}])),
    ]
    for name, fn in checks:
        try:
            fn()
        except Exception:
            errors.append(f"{name}: " + traceback.format_exc())

    # force a draw of every custom Cairo widget
    import cairo
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 400, 400)
    cr = cairo.Context(surface)
    for name, widget, handler in [
            ("Face", win.face, win.face.on_draw),
            ("Waveform", win.waveform, win.waveform.on_draw),
            ("MemoryGraph", win.memory_graph, win.memory_graph.on_draw),
            ("CameraOverlay", win.camera_overlay.drawing, win.camera_overlay.on_draw),
            ("RingFlash", ring.drawing, ring.on_draw)]:
        try:
            widget.set_size_request(400, 400)
            handler(widget, cr)
        except Exception:
            errors.append(f"draw {name}: " + traceback.format_exc())

# -- pinch to point, driven through the real hand-frame callback -----------
if win is not None:
    def hand(index_tip, thumb_tip=(0.9, 0.9)):
        """A 21-point hand with the wrist and knuckle fixed, so the hand size
        used for the pinch threshold is a known 0.2."""
        points = [(0.5, 0.8)] * 21
        points[0] = (0.5, 0.8)    # wrist
        points[9] = (0.5, 0.6)    # middle knuckle -> hand size 0.2
        points[4] = thumb_tip
        points[8] = index_tip
        return points

    aimed = []
    clicks = []
    app.pointer_driver.aim = lambda x, y: aimed.append((x, y))
    app.pointer_driver.request_click = lambda: clicks.append(True)
    app.pointer_driver.start = lambda: None
    app.pointer_driver.stop = lambda: None

    try:
        # nothing happens until it is armed
        win._pinch_cursor_armed = False
        win.on_camera_hand_frame([hand((0.5, 0.5))])
        if aimed or clicks:
            errors.append("hand pointing moved the cursor before being armed")

        # arm it by hand, bypassing the consent prompt (covered separately)
        app.screen_control.grant()
        win._arm_pinch_cursor()
        if not win._pinch_cursor_armed:
            errors.append("arming hand pointing after consent did not take effect")

        # a hand at the centre of the frame aims at the centre of the screen
        win._pointer_smoothed = None
        for _ in range(40):
            win.on_camera_hand_frame([hand((0.5, 0.5))])
        if not aimed:
            errors.append("an armed, visible hand aimed the pointer nowhere")
        else:
            x, y = aimed[-1]
            if not (0.45 < x < 0.55 and 0.45 < y < 0.55):
                errors.append(f"a centred hand should aim at the centre, aimed at {(x, y)}")

        # the camera image is mirrored: a hand on the left of the frame is the
        # user's right hand side, and the cursor should follow the user
        win._pointer_smoothed = None
        for _ in range(40):
            win.on_camera_hand_frame([hand((0.2, 0.5))])
        mirrored_x = aimed[-1][0]
        if mirrored_x < 0.6:
            errors.append(f"pointing is not mirrored: frame x 0.2 aimed at {mirrored_x}")

        # movement is smoothed rather than snapping straight to the target
        win._pointer_smoothed = None
        win.on_camera_hand_frame([hand((0.5, 0.5))])
        first = aimed[-1]
        win.on_camera_hand_frame([hand((0.5, 0.1))])
        second = aimed[-1]
        if abs(second[1] - first[1]) > 0.35:
            errors.append("pointer movement is not being smoothed between frames")

        # pinching clicks once, not once per frame
        clicks.clear()
        win._pinch_held = False
        win._last_pinch_click = 0.0
        for _ in range(10):
            win.on_camera_hand_frame([hand((0.5, 0.5), thumb_tip=(0.5, 0.52))])
        if len(clicks) != 1:
            errors.append(f"a held pinch should click once, clicked {len(clicks)} times")

        # opening the hand again re-arms the click
        for _ in range(5):
            win.on_camera_hand_frame([hand((0.5, 0.5), thumb_tip=(0.9, 0.9))])
        if win._pinch_held:
            errors.append("opening the hand did not release the pinch")

        # no hand in view means no pointer movement
        before = len(aimed)
        win.on_camera_hand_frame([])
        if len(aimed) != before:
            errors.append("an empty frame still moved the pointer")

        # turning it off stands everything down
        win._disarm_pinch_cursor()
        before = len(aimed)
        win.on_camera_hand_frame([hand((0.5, 0.5))])
        if len(aimed) != before:
            errors.append("the pointer still moved after hand pointing was turned off")
    except Exception:
        errors.append("pinch to point: " + traceback.format_exc())
    finally:
        app.screen_control.deny()

# -- the Dynamic Island's notification card --------------------------------
if win is not None:
    try:
        pressed = []
        win.island.show_card("Toby has a reply", "A short preview of the answer.",
                             [("Open", lambda: pressed.append("open")),
                              ("Dismiss", lambda: pressed.append("dismiss"))])
        if not win.island.showing_card():
            errors.append("the island did not go into card mode")
        buttons = win.island.card_actions.get_children()
        if len(buttons) != 2:
            errors.append(f"the card should show 2 buttons, showed {len(buttons)}")
        else:
            buttons[0].emit("clicked")
            if pressed != ["open"]:
                errors.append(f"pressing a card button ran the wrong thing: {pressed}")
            if win.island.showing_card():
                errors.append("the card stayed open after its button was pressed")

        # a card that asks to stay open should stay open
        win.island.show_card("Still working", "",
                             [("Keep", lambda: "keep")])
        win.island.card_actions.get_children()[0].emit("clicked")
        if not win.island.showing_card():
            errors.append("a card that returned 'keep' closed anyway")

        # a failing action must not take the card down with it
        def explode():
            raise RuntimeError("deliberate")

        win.island.show_card("Careful", "", [("Boom", explode)])
        win.island.card_actions.get_children()[0].emit("clicked")

        # going back to the plain status pill drops the card
        win.island.hide_island()
        if win.island.showing_card():
            errors.append("hiding the island left it in card mode")
        if win.island.card_actions.get_children():
            errors.append("hiding the island left the card's buttons behind")

        # the real path: a finished reply with the panel closed
        win.set_visible(False)
        win._show_reply_card("what is this", "Here is a reply. " * 40)
        if not win.island.showing_card():
            errors.append("a finished reply with the panel closed showed no card")
        labels = [b.get_label() for b in win.island.card_actions.get_children()]
        if labels != ["Open", "Explain", "Later", "Dismiss"]:
            errors.append(f"unexpected card actions: {labels}")
        body = win.island.card_body.get_text()
        if len(body) > 230:
            errors.append(f"the reply preview was not shortened ({len(body)} chars)")
        win.island.hide_island()
    except Exception:
        errors.append("island card: " + traceback.format_exc())

# pure-logic checks that need no widgets at all
def expect(label, got, want):
    if got != want:
        errors.append(f"{label}: got {got!r}, want {want!r}")

expect("extract_partial_reply basic",
       app.extract_partial_reply('{"reply": "hello wor'), "hello wor")
expect("extract_partial_reply escapes",
       app.extract_partial_reply('{"reply": "line\\none \\"q\\""}'), 'line\none "q"')
expect("extract_partial_reply absent",
       app.extract_partial_reply('{"actions": []'), None)
expect("parse fenced json",
       app._parse_llm_json_reply('```json\n{"reply":"hi"}\n```')["reply"], "hi")
expect("parse garbage falls back to text",
       app._parse_llm_json_reply("not json at all")["reply"], "not json at all")

if errors:
    print(f"\n{len(errors)} PROBLEM(S):\n")
    for e in errors:
        print(e)
        print("-" * 60)
    sys.exit(1)
print("smoke test passed")
