"""Run the app's stylesheet through the real GTK3 CSS parser.

This matters more than it looks. When GTK hits a property it does not
understand, it drops that declaration and carries on silently. The app still
starts, the style just quietly does nothing — a miserable thing to debug by
eye, and the reason the old stylesheet accumulated overlapping rules. The
real parser turns that into an immediate, specific error.

The sheet is generated per accent colour, so every accent the user could
plausibly set is checked, including malformed input.

Run it with:  ./tests/run.sh
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _layer_shell_stub  # noqa: E402

_layer_shell_stub.install()

from gi.repository import Gtk  # noqa: E402

import linux_agent_apple as app  # noqa: E402

problems = []


def parse(css_bytes, label):
    found = []
    provider = Gtk.CssProvider()

    def on_error(_provider, section, error):
        found.append(f"{label}, line {section.get_start_line() + 1}: {error.message}")

    provider.connect("parsing-error", on_error)
    try:
        provider.load_from_data(css_bytes)
    except Exception as e:
        found.append(f"{label}: refused to load: {e}")
    problems.extend(found)
    return found


sheet = app.build_css()
parse(sheet.encode(), "default accent")
print(f"stylesheet: {len(sheet)} chars, {sheet.count('{')} rules")

# Every accent a user might set, including ones that are not colours at all.
for accent in ["#5a8cff", "#ff4d6d", "#00e0a4", "#ffffff", "#000000",
               "#7b61ff", "", "not-a-color", "#12345", None]:
    parse(app.build_css(accent).encode(), f"accent {accent!r}")

# Every class the code actually applies must exist in the sheet, or the
# widget silently falls back to the system theme.
applied = set()
source = open(os.path.join(_layer_shell_stub.REPO_ROOT,
                           "scripts", "linux_agent_apple.py")).read()
import re  # noqa: E402

for match in re.findall(r'add_class\(\s*f?"([^"{}]+)"', source):
    applied.add(match)
# the two built by interpolation
applied.update({f"apple-agent-step-{s}" for s in ("done", "current", "pending", "error")})
applied.update({f"apple-agent-topic-{t}" for t in ("none", "low", "mid", "high")})

defined = set(re.findall(r"\.([a-zA-Z][\w-]*)", sheet))
missing = sorted(c for c in applied - defined if c != "chat")
if missing:
    problems.append("classes used in code but absent from the stylesheet: "
                    + ", ".join(missing))

if problems:
    print(f"{len(problems)} PROBLEM(S):")
    for problem in problems:
        print("  ", problem)
    sys.exit(1)
print("stylesheet parses clean for every accent, and covers every class used")
