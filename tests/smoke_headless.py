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
import os, sys, types, traceback

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import gi
_real_require = gi.require_version
def require_version(ns, ver):
    if ns == "GtkLayerShell":
        return
    return _real_require(ns, ver)
gi.require_version = require_version

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa

class _Edge:
    TOP = "top"; BOTTOM = "bottom"; LEFT = "left"; RIGHT = "right"
class _Layer:
    OVERLAY = "overlay"; TOP = "top"
class _KeyboardMode:
    NONE = "none"; ON_DEMAND = "on_demand"; EXCLUSIVE = "exclusive"

stub = types.ModuleType("gi.repository.GtkLayerShell")
stub.Edge = _Edge
stub.Layer = _Layer
stub.KeyboardMode = _KeyboardMode
_margins = {}
stub.init_for_window = lambda w: None
stub.set_layer = lambda w, l: None
stub.set_namespace = lambda w, n: None
stub.set_anchor = lambda w, e, v: None
stub.set_margin = lambda w, e, v: _margins.__setitem__((id(w), e), v)
stub.get_margin = lambda w, e: _margins.get((id(w), e), 0)
stub.set_keyboard_mode = lambda w, m: None
stub.set_exclusive_zone = lambda w, z: None

import gi.repository
sys.modules["gi.repository.GtkLayerShell"] = stub
gi.repository.GtkLayerShell = stub

sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
import linux_agent_apple as app

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
