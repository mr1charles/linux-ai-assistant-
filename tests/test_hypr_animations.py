"""Runtime Hyprland animations, against a stand-in hyprctl."""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import hypr_animations as ha  # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


class Done:
    def __init__(self, code, out):
        self.returncode, self.stdout, self.stderr = code, out, ""


def plain_hyprctl(calls):
    def run(argv, **_k):
        calls.append(argv[1:])
        return Done(0, "ok")
    return run


def lua_hyprctl(calls):
    """Rejects unquoted keyword values, like a Lua config provider can."""
    def run(argv, **_k):
        calls.append(argv[1:])
        if argv[1] == "keyword" and not argv[3].startswith('"'):
            return Done(0, "error: could not parse")
        return Done(0, "ok")
    return run


pairs = ha.build_keywords(1.0)
check("three curves then every animation", len(pairs), len(ha.BEZIERS) + len(ha.ANIMATIONS))
check("curves come first, so animations can name them", pairs[0][0], "bezier")
check("a window animation is built right", ("animation", "windowsIn, 1, 4.0, tobyOut, popin 86%") in pairs, True)
check("speed scales durations", ("animation", "windowsIn, 1, 2.0, tobyOut, popin 86%") in ha.build_keywords(0.5), True)
check("speed is clamped", ha.build_keywords(99) == ha.build_keywords(4.0), True)

calls = []
applied, failed = ha.apply(1.0, runner=plain_hyprctl(calls))
check("everything applies on a normal Hyprland", (len(applied), failed), (len(pairs), []))
check("one call each", len(calls), len(pairs))

calls = []
applied, failed = ha.apply(1.0, runner=lua_hyprctl(calls))
check("a Lua-config Hyprland gets the quoted form", (len(applied), failed), (len(pairs), []))
check("only after the plain form was refused", calls[0][2].startswith('"'), False)


def missing(argv, **_k):
    raise FileNotFoundError


applied, failed = ha.apply(1.0, runner=missing)
check("without Hyprland nothing is claimed as applied", applied, [])


def report(argv, **_k):
    return Done(0, json.dumps([[{"name": "windowsIn", "bezier": "tobyOut"},
                                {"name": "border", "bezier": "default"}], []]))


check("verification reads back what's active", ha.active_toby_animations(runner=report), {"windowsIn"})
check("restoring reloads the config", ha.restore(runner=plain_hyprctl([])), True)

if failures:
    print(f"{len(failures)} PROBLEM(S):")
    for f in failures:
        print("  ", f)
    sys.exit(1)
print("hyprland animation checks passed")
