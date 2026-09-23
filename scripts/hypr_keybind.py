"""
hypr_keybind.py — give Toby its summon key without editing your config.

At startup Toby asks the running Hyprland to bind Super+G (or whatever
"summon_keybind" says in settings.json) to summon it. Like the animations,
this is a runtime binding only: it's gone when Hyprland reloads its config,
and Toby simply adds it again next time it starts. Your keybinds.lua or
hyprland.conf is never opened.

If the key is already bound to something else, Toby leaves it alone rather
than stacking a second action on your key, and `toby doctor` says so.
"""

import json
import subprocess

MODMASKS = {"SHIFT": 1, "CAPS": 2, "CTRL": 4, "CONTROL": 4, "ALT": 8, "MOD2": 16,
            "MOD3": 32, "SUPER": 64, "WIN": 64, "LOGO": 64, "MOD4": 64, "MOD5": 128}
SUMMON = "pkill -SIGUSR1 -f linux_agent_apple.py"


def parse(combo):
    """"SUPER, G" or "SUPER SHIFT, T" -> (modmask, key)."""
    mods, _, key = combo.partition(",")
    mask = 0
    for mod in mods.replace("+", " ").split():
        mask |= MODMASKS.get(mod.strip().upper(), 0)
    return mask, key.strip()


def _run(args, runner):
    try:
        out = runner(["hyprctl"] + args, capture_output=True, text=True, timeout=2.0)
        return out.returncode, (out.stdout or "") + (out.stderr or "")
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def existing_binding(combo, runner=subprocess.run):
    """What the key currently does, or None if it's free."""
    mask, key = parse(combo)
    code, out = _run(["-j", "binds"], runner)
    if code != 0:
        return None
    try:
        binds = json.loads(out)
    except ValueError:
        return None
    for bind in binds if isinstance(binds, list) else []:
        if int(bind.get("modmask", -1)) == mask and str(bind.get("key", "")).lower() == key.lower():
            return f"{bind.get('dispatcher', '')} {bind.get('arg', '')}".strip()
    return None


def ensure(combo="SUPER, G", runner=subprocess.run):
    """Bind the summon key if it's free. Returns a short status string."""
    current = existing_binding(combo, runner)
    if current is not None:
        if "linux_agent_apple" in current or current.endswith(" toby"):
            return "already bound"
        return f"taken by: {current}"
    value = f"{combo}, exec, {SUMMON}"
    for attempt in (value, f'"{value}"'):
        code, out = _run(["keyword", "bind", attempt], runner)
        if code == 0 and "error" not in out.lower():
            return "bound"
    return "failed"
