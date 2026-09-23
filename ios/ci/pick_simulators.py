#!/usr/bin/env python3
"""Pick a small and a large iPhone simulator on this build machine.

Prints "udid name" lines: the smallest-screen iPhone available (an iPhone SE
if there is one) and the largest (a Pro Max), on the newest iOS runtime, so
the UI tests cover both ends of iPhone screen sizes.
"""
import json
import subprocess

out = subprocess.run(["xcrun", "simctl", "list", "devices", "available", "-j"],
                     capture_output=True, text=True, check=True).stdout
runtimes = json.loads(out)["devices"]
ios = sorted((r for r in runtimes if "iOS" in r), key=lambda r: [int(x) for x in r.rsplit("iOS-", 1)[-1].split("-")])
chosen = []
for runtime in reversed(ios):
    phones = [d for d in runtimes[runtime] if d["name"].startswith("iPhone")]
    if not phones:
        continue
    small = next((d for d in phones if "SE" in d["name"]), None) or \
        next((d for d in phones if d["name"].endswith(("mini", "16e", "17e"))), None) or phones[0]
    large = next((d for d in phones if "Pro Max" in d["name"]), None) or \
        next((d for d in phones if "Plus" in d["name"] or "Max" in d["name"]), None) or phones[-1]
    chosen = [small] + ([large] if large["udid"] != small["udid"] else [])
    break
for device in chosen:
    print(device["udid"], device["name"])
