"""Notes inside the real app: the prompt, the tools, the permission levels."""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _layer_shell_stub  # noqa: E402

_layer_shell_stub.install()
import linux_agent_apple as app  # noqa: E402
import permissions  # noqa: E402


def check(condition, what):
    if not condition:
        raise AssertionError(what)
    print("  ok:", what)


vault = Path(tempfile.mkdtemp(prefix="toby-vault-"))
(vault / "Dentist.md").write_text("# Dentist\n\n## Appointments\nNext check-up is Tuesday 14 October at 3pm.\n")

app.SETTINGS["notes_folder"] = ""
check("From the user's own notes" not in app._build_system_content("when is the dentist?"),
      "no notes folder: nothing from notes in the prompt")
check("toby notes set" in app.DISPATCH["search_notes"]({"query": "dentist"}),
      "and search_notes says how to set one up rather than pretending")

app.SETTINGS["notes_folder"] = str(vault)
app.knowledge.use_notes(app.notes_root)
app.notes_index()
deadline = time.time() + 10
while app.notes_index() is None and time.time() < deadline:
    time.sleep(0.05)
check(app.notes_index() is not None, "the notes folder is read in the background")

prompt = app._build_system_content("when is my dentist appointment?")
check(prompt.startswith(app.SYSTEM_PROMPT_STATIC), "the static half of the prompt is untouched (cache stays warm)")
tail = prompt[len(app.SYSTEM_PROMPT_STATIC):]
check("Dentist › Appointments" in tail and "14 October" in tail, "the matching passage is in the volatile half")
check("From the user's own notes" not in app._build_system_content("open youtube"),
      "an unrelated request carries nothing from the notes")
check("From the user's own notes" not in app._build_system_content(), "follow-up rounds without a query carry nothing")

found = app.DISPATCH["search_notes"]({"query": "dentist tuesday"})
check("Dentist.md" in found and "14 October" in found, "search_notes returns the passage and its note")
said = app.DISPATCH["remember"]({"text": "Has a dentist called Dr Lee", "category": "health"})
check("Toby/Memory.md" in said and "Dr Lee" in (vault / "Toby" / "Memory.md").read_text(), "remember writes to the notes")
saved = app.DISPATCH["add_note"]({"title": "Questions for the dentist", "text": "Ask about whitening."})
check("Toby/Notes/Questions for the dentist.md" in saved, "add_note saves in Toby/Notes")
for tool in ("search_notes", "remember", "add_note"):
    check(permissions.classify({"tool": tool, "query": "x", "text": "x", "title": "x"}).level == permissions.SAFE,
          f"{tool} doesn't need asking (it only reads, or adds to Toby's own pages)")
    check(tool in app.SYSTEM_PROMPT_STATIC, f"the model is told about {tool}")
check(app.describe_action({"tool": "search_notes", "query": "dentist"}) == 'Search your notes for "dentist"',
      "the checklist says it in words")
print("notes-in-the-app checks passed")
