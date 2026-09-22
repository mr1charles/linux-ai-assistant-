"""
hypr_animations.py — window, workspace and menu motion that matches Toby.

Windows opening and closing, workspaces sliding, the special workspace
(Hyprland's stand-in for minimising) and layer surfaces such as menus and
notifications are all animated by Hyprland itself, so that's where these
have to live. They share Toby's curves — a quick start with a long, soft
settle, and no overshoot — so the whole desktop moves as one thing.

Nothing is written to your config. The curves are applied to the running
compositor with ``hyprctl keyword``, which lasts until Hyprland reloads its
config or restarts, at which point exactly what you had comes back. `toby
animations off` does that reload for you. That also means this can never
leave your Caelestia/Noctalia setup in a state it can't recover from.

It's opt-in (`toby animations on`), and it checks its own work: after
applying, it asks Hyprland which animations are now active and reports
honestly if the compositor didn't take them — which can happen with Lua
config providers that parse keyword arguments differently.
"""

import json
import subprocess

# Bezier control points. Named toby* so they're recognisable in
# `hyprctl animations` and never collide with the shell's own curves.
BEZIERS = {
    "tobyOut": (0.16, 1.0, 0.3, 1.0),     # arrivals: fast, then a long settle
    "tobyIn": (0.55, 0.0, 0.8, 0.2),      # departures: gather, then go
    "tobyMove": (0.33, 1.0, 0.68, 1.0),   # things travelling between places
}

# (animation, speed in Hyprland's units of 100 ms, bezier, style)
ANIMATIONS = [
    ("windowsIn", 4.0, "tobyOut", "popin 86%"),
    ("windowsOut", 3.0, "tobyIn", "popin 90%"),
    ("windowsMove", 4.0, "tobyMove", ""),
    ("fadeIn", 3.0, "tobyOut", ""),
    ("fadeOut", 2.5, "tobyIn", ""),
    ("workspaces", 5.0, "tobyMove", "slidefade 12%"),
    ("specialWorkspace", 4.0, "tobyMove", "slidefadevert 14%"),
    ("layersIn", 3.0, "tobyOut", "popin 94%"),
    ("layersOut", 2.0, "tobyIn", "fade"),
]


def build_keywords(speed=1.0):
    """The (keyword, value) pairs to apply. speed < 1 is faster."""
    speed = max(0.2, min(4.0, float(speed)))
    pairs = []
    for name, (x0, y0, x1, y1) in BEZIERS.items():
        pairs.append(("bezier", f"{name}, {x0}, {y0}, {x1}, {y1}"))
    for name, base, curve, style in ANIMATIONS:
        value = f"{name}, 1, {round(base * speed, 2)}, {curve}"
        if style:
            value += f", {style}"
        pairs.append(("animation", value))
    return pairs


def _run(args, runner):
    try:
        out = runner(["hyprctl"] + args, capture_output=True, text=True, timeout=2.0)
        return out.returncode, (out.stdout or "") + (out.stderr or "")
    except FileNotFoundError:
        return 127, "hyprctl not found"
    except subprocess.TimeoutExpired:
        return 124, "hyprctl timed out"


def _accepted(code, output):
    text = output.strip().lower()
    return code == 0 and (text in ("", "ok") or text.endswith("ok")) and "error" not in text


def apply(speed=1.0, runner=subprocess.run):
    """Apply every curve. Returns (applied, failed) lists of keyword values.

    Each keyword is tried as plain text first. If Hyprland rejects it, it's
    tried once more wrapped in quotes — the form Lua-based config providers
    need, where an unquoted argument is read as an expression.
    """
    applied, failed = [], []
    for keyword, value in build_keywords(speed):
        code, output = _run(["keyword", keyword, value], runner)
        if not _accepted(code, output):
            code, output = _run(["keyword", keyword, f'"{value}"'], runner)
        (applied if _accepted(code, output) else failed).append(value)
    return applied, failed


def active_toby_animations(runner=subprocess.run):
    """Names of animations currently using one of Toby's curves, per Hyprland."""
    code, output = _run(["-j", "animations"], runner)
    if code != 0:
        return set()
    try:
        data = json.loads(output)
    except ValueError:
        return set()
    # `hyprctl -j animations` returns [animations, beziers]
    entries = data[0] if isinstance(data, list) and data and isinstance(data[0], list) else data
    names = set()
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and str(entry.get("bezier", "")).startswith("toby"):
            names.add(entry.get("name"))
    return names


def restore(runner=subprocess.run):
    """Put back exactly what your config says, by reloading it."""
    code, output = _run(["reload"], runner)
    return _accepted(code, output)
