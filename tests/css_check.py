"""Parse the app's CSS block with the real GTK3 CSS parser and report anything
GTK complains about.

This matters more than it looks: when GTK hits a property it doesn't
understand, it drops that declaration (and sometimes the rest of the rule)
and carries on silently. The app still starts, the style just quietly does
nothing — which is a miserable thing to debug by eye. Running the real
parser turns that into an immediate, specific error.

Run it with:  ./tests/run.sh
"""
import os, sys
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

DEFAULT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "scripts", "linux_agent_apple.py")
src = open(sys.argv[1] if len(sys.argv) > 1 else DEFAULT).read()
start = src.index('CSS = b"""') + len('CSS = b"""')
end = src.index('"""', start)
css = src[start:end].encode()

problems = []
prov = Gtk.CssProvider()
def on_error(provider, section, error):
    problems.append(f"line {section.get_start_line()+1}: {error.message}")
prov.connect("parsing-error", on_error)
try:
    prov.load_from_data(css)
except Exception as e:
    problems.append(f"fatal: {e}")

print(f"CSS block: {len(css)} bytes, {css.count(b'{')} rules")
if problems:
    print(f"{len(problems)} PARSE PROBLEM(S):")
    for p in problems:
        print("  ", p)
    sys.exit(1)
print("CSS parses clean.")
