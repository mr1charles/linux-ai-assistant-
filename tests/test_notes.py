"""Your notes folder as Toby's memory (scripts/notes.py, knowledge.py).

A sample vault with ordinary notes, private folders, a .tobyignore, and a
note holding secrets: search finds the right passage, never reads what's off
limits, never shows a secret, keeps up as files change, and Toby's memory
lives in (and follows edits to) Toby/Memory.md.
"""

import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import knowledge  # noqa: E402
import notes  # noqa: E402


def check(condition, what):
    if not condition:
        raise AssertionError(what)
    print("  ok:", what)


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


work = Path(tempfile.mkdtemp(prefix="toby-notes-"))
vault = work / "Vault"
write(vault, "Dentist.md", "---\ntags: [health-admin]\n---\n# Dentist\n\n## Appointments\n"
      "Next check-up is Tuesday 14 October at 3pm with Dr Lee, on Harbour Street.\n\n## Old\nCleaning in March.\n")
write(vault, "Projects/Garden.md", "# Garden\n\n" + "\n".join(
    f"Row {i}: tomatoes and basil, watered every morning, compost added in spring." for i in range(40)))
write(vault, "Projects/Car.md", "# Car\n\nThe car's MOT is due in November. Tyres changed in May.\n")
write(vault, "Wifi.md", "# Home network\n\nThe wifi password: hunter2-orange\nRouter admin api_key=abc123def456\n"
      "OpenAI key sk-proj-ABCDEFGHIJKLMNOPQRSTUV1234 lives here too.\n")
write(vault, "Health/Meds.md", "# Medication\n\nTake the blue pills with breakfast.\n")
write(vault, "Private Stuff/Diary.md", "# Diary\n\nToday I told nobody about the surprise party.\n")
write(vault, "Journal/2026-09-01.md", "# Journal\n\nFelt great about the marathon training today.\n")
write(vault, ".obsidian/workspace.md", "the workspace layout marathon\n")
write(vault, "Toby/Log/2026-09-01.md", "- 10:00 checked the dentist thing\n")
write(vault, ".tobyignore", "# keep these to myself\nJournal/\n*.draft.md\n")
write(vault, "Letter.draft.md", "# Draft\n\nDear marathon committee...\n")

index_path = work / "notes_index.sqlite"
index = notes.NotesIndex(vault, index_path)
index.refresh(force=True)
names = set(index.notes())
check(names == {"Dentist.md", "Projects/Garden.md", "Projects/Car.md", "Wifi.md"},
      f"only ordinary notes are read (got {sorted(names)})")
check(not any(n.startswith(("Health", "Private", "Journal", ".obsidian", "Toby/Log")) for n in names),
      "private-looking folders, hidden folders, .tobyignore'd files and Toby's own log are skipped")

hits = index.search("when is my next dentist appointment")
check(hits and hits[0]["rel"] == "Dentist.md" and "14 October" in hits[0]["snippet"],
      "the right note and passage come first")
check(hits[0]["heading"] == "Appointments", "the passage knows which heading it's under")
check(not index.search("blue pills breakfast") and not index.search("surprise party") and
      not index.search("marathon"), "nothing off limits can be found at all")

wifi = index.search("wifi password router key")
text = " ".join(h["snippet"] for h in wifi)
check(wifi and "hunter2" not in text and "abc123def456" not in text and "sk-proj" not in text
      and "[hidden]" in text, "passwords and keys are blanked out of what's shown")
check(notes.redact("the secret is to add salt") == "the secret is to add salt",
      "ordinary sentences with those words are left alone")

garden = index.passages_of("Projects/Garden.md")
check(len(garden) > 1 and all(len(c["text"]) <= notes.CHUNK_CHARS for c in garden),
      "long notes are split into passages of a readable size")

# -- keeping up with changes ---------------------------------------------------------------
check(stat.S_IMODE(index_path.stat().st_mode) == 0o600, "the index (it holds your notes) is readable by you alone")
reloaded = notes.NotesIndex(vault, index_path)
check(set(reloaded.notes()) == names and reloaded.refresh(force=True) == 0,
      "the index is kept on disk, so a restart doesn't re-read anything unchanged")
time.sleep(0.05)
write(vault, "Projects/Car.md", "# Car\n\nThe car's MOT is due in December now. New wipers fitted.\n")
os.utime(vault / "Projects/Car.md", (time.time() + 5, time.time() + 5))
(vault / "Wifi.md").unlink()
changed = index.refresh(force=True)
check(changed == 2, "one edited and one deleted note are picked up")
check("December" in index.search("MOT due")[0]["snippet"], "an edited note is searched as it is now")
check("Wifi.md" not in index.notes() and not index.search("hunter2 orange"), "a deleted note is forgotten")
check(index.refresh() == 0, "and nothing is re-read until it changes (or a minute passes)")

# -- what goes in front of the model -----------------------------------------------------------
block = notes.context_for(index, "what time is the dentist on tuesday?", budget=600)
check(block.startswith("From the user's own notes") and "Dentist › Appointments" in block and len(block) <= 700,
      "a matching question brings the passage, named, within budget")
check(notes.context_for(index, "open youtube") == "", "an unrelated request brings nothing")
check(notes.context_for(index, "good morning") == "" and notes.context_for(index, "hello") == "",
      "greetings don't drag in a note that happens to share a word")
check("Garden" in notes.context_for(index, "when do I water the tomatoes and basil?"),
      "but a real question about something in the notes does")
check(notes.context_for(None, "dentist") == "", "no folder, nothing")

# -- Toby's memory, in the notes -------------------------------------------------------------------
knowledge.KNOWLEDGE_PATH = work / "knowledge.json"
knowledge.KNOWLEDGE_PATH.write_text(json.dumps({"facts": [
    {"id": "f0001", "text": "Plays soccer on weekends", "category": "hobbies", "added": "2026-01-01T00:00:00"},
    {"id": "f0002", "text": "Dislikes cilantro", "category": "food", "added": "2026-01-02T00:00:00"}]}))
knowledge.use_notes(lambda: vault)
memory = vault / "Toby" / "Memory.md"
check(memory.exists() and "## hobbies" in memory.read_text() and "- Dislikes cilantro" in memory.read_text(),
      "facts Toby already knew are copied into Toby/Memory.md, grouped")
check("Dislikes cilantro" in knowledge.get_facts_summary(), "and Toby reads them from there")
added = knowledge.add_fact_manual("Prefers tea to coffee", "food")
check(added and "- Prefers tea to coffee" in memory.read_text(), "new facts are written to the notes")
check(knowledge.add_fact_manual("Prefers tea to coffee", "food") is None, "and not twice")
memory.write_text(memory.read_text().replace("- Dislikes cilantro\n", ""))
check("cilantro" not in knowledge.get_facts_summary(), "a line you delete in your notes app is forgotten")
graph = knowledge.get_graph_data()
check({n["label"] for n in graph["nodes"]} == {"Plays soccer on weekends", "Prefers tea to coffee"},
      "the Memories graph shows what's in the file")
check(knowledge.delete_fact(added["id"]) and "tea" not in memory.read_text(), "deleting in Toby deletes the line")
check(json.loads(knowledge.KNOWLEDGE_PATH.read_text())["facts"][1]["text"] == "Dislikes cilantro",
      "knowledge.json is left as it was, as a backup")
index.refresh(force=True)
check("Toby/Memory.md" not in index.notes() and not index.search("soccer weekends"),
      "Memory.md isn't searched as a note (it's already in Toby's context)")
knowledge.use_notes(lambda: None)
check("cilantro" in knowledge.get_facts_summary(), "without a notes folder, knowledge.json is used again")

# -- notes you ask for, and the daily log ------------------------------------------------------------
path = notes.add_note(vault, "Gift ideas / Mum", "A good teapot.")
check(path == vault / "Toby" / "Notes" / "Gift ideas Mum.md" and "A good teapot." in path.read_text(),
      "a note you ask for goes in Toby/Notes, with a safe file name")
notes.add_note(vault, "Gift ideas / Mum", "Or a scarf.")
check(path.read_text().count("teapot") == 1 and "Or a scarf." in path.read_text(), "asking again adds to it")
log = notes.log(vault, "run the tests in my project: All 5 passed.")
check(log.parent == vault / "Toby" / "Log" and "run the tests" in log.read_text() and log.read_text().startswith("# Toby, "),
      "each task gets a line in today's log")
index.refresh(force=True)
check("Toby/Notes/Gift ideas Mum.md" in index.notes() and not any(k.startswith("Toby/Log") for k in index.notes()),
      "saved notes are searchable; the log isn't")
print("notes checks passed")
