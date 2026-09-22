"""Shared setup for tests that need to import linux_agent_apple.

The module builds real GTK widget subclasses at import time, so real GTK has
to be present — but GtkLayerShell only means anything under a Wayland
compositor, and there isn't one here. Replacing just that one namespace with
a no-op stub is enough to import and construct the whole app under Xvfb.

Import this before importing linux_agent_apple.
"""
import os
import sys
import types

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def install():
    """Stub GtkLayerShell, put scripts/ on the path, and return the repo root."""
    import gi

    real_require_version = gi.require_version

    def require_version(namespace, version):
        if namespace == "GtkLayerShell":
            return
        return real_require_version(namespace, version)

    gi.require_version = require_version
    gi.require_version("Gtk", "3.0")

    class Edge:
        TOP = "top"
        BOTTOM = "bottom"
        LEFT = "left"
        RIGHT = "right"

    class Layer:
        OVERLAY = "overlay"
        TOP = "top"

    class KeyboardMode:
        NONE = "none"
        ON_DEMAND = "on_demand"
        EXCLUSIVE = "exclusive"

    margins = {}
    stub = types.ModuleType("gi.repository.GtkLayerShell")
    stub.Edge = Edge
    stub.Layer = Layer
    stub.KeyboardMode = KeyboardMode
    stub.init_for_window = lambda window: None
    stub.set_layer = lambda window, layer: None
    stub.set_namespace = lambda window, name: None
    stub.set_anchor = lambda window, edge, anchored: None
    stub.set_margin = lambda window, edge, value: margins.__setitem__((id(window), edge), value)
    stub.get_margin = lambda window, edge: margins.get((id(window), edge), 0)
    stub.set_keyboard_mode = lambda window, mode: None
    stub.set_exclusive_zone = lambda window, zone: None
    stub.set_monitor = lambda window, monitor: None

    import gi.repository

    sys.modules["gi.repository.GtkLayerShell"] = stub
    gi.repository.GtkLayerShell = stub

    scripts_dir = os.path.join(REPO_ROOT, "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    return REPO_ROOT
