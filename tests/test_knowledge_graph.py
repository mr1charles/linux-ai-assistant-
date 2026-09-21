"""Checks on the three Memories graph layouts.

A layout can fail quietly in ways a smoke test won't notice: positions
drifting outside the box the view draws in, every node landing on the same
spot, or the arrangement reshuffling itself between visits so the picture is
never twice the same. These check the geometry directly.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

workdir = tempfile.mkdtemp()
os.makedirs(os.path.join(workdir, "linux-agent"), exist_ok=True)
os.environ["HOME"] = workdir

import knowledge  # noqa: E402
from pathlib import Path  # noqa: E402

knowledge.KNOWLEDGE_PATH = Path(workdir) / "linux-agent" / "knowledge.json"

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def check_true(label, got):
    if not got:
        failures.append(f"{label}: expected true, got {got!r}")


def write_facts(count_per_category, categories=("school", "preferences", "family")):
    facts = []
    for category in categories:
        for i in range(count_per_category):
            facts.append({"id": f"{category}-{i}",
                          "text": f"A fact about {category} number {i}",
                          "category": category,
                          "added": "2026-01-01T00:00:00"})
    knowledge.KNOWLEDGE_PATH.write_text(json.dumps({"facts": facts}))
    return facts


# -- nothing learned yet -----------------------------------------------------
knowledge.KNOWLEDGE_PATH.write_text(json.dumps({"facts": []}))
for layout in knowledge.LAYOUTS:
    empty = knowledge.get_graph_data(layout)
    check(f"{layout}: no facts gives no nodes", empty["nodes"], [])
    check(f"{layout}: no facts gives no edges", empty["edges"], [])

# -- a single fact -----------------------------------------------------------
write_facts(1, categories=("school",))
for layout in knowledge.LAYOUTS:
    single = knowledge.get_graph_data(layout)
    check(f"{layout}: one fact gives one node", len(single["nodes"]), 1)
    node = single["nodes"][0]
    check_true(f"{layout}: a lone node sits inside the view",
               0.0 <= node["x"] <= 1.0 and 0.0 <= node["y"] <= 1.0)

# -- a realistic set ---------------------------------------------------------
facts = write_facts(5)

for layout in knowledge.LAYOUTS:
    data = knowledge.get_graph_data(layout)
    check(f"{layout}: every fact appears", len(data["nodes"]), len(facts))

    inside = all(0.0 <= n["x"] <= 1.0 and 0.0 <= n["y"] <= 1.0 for n in data["nodes"])
    check_true(f"{layout}: every node is inside the 0..1 view box", inside)

    spots = {(round(n["x"], 3), round(n["y"], 3)) for n in data["nodes"]}
    check_true(f"{layout}: nodes are spread out rather than stacked",
               len(spots) >= len(facts) - 1)

    # the same facts must produce the same picture every time
    again = knowledge.get_graph_data(layout)
    check(f"{layout}: the arrangement is stable between calls",
          [(n["id"], round(n["x"], 6), round(n["y"], 6)) for n in data["nodes"]],
          [(n["id"], round(n["x"], 6), round(n["y"], 6)) for n in again["nodes"]])

    check_true(f"{layout}: labels and categories are carried through",
               all(n["label"] and n["category"] for n in data["nodes"]))

# -- the three layouts are genuinely different -------------------------------
arrangements = {}
for layout in knowledge.LAYOUTS:
    data = knowledge.get_graph_data(layout)
    arrangements[layout] = [(n["id"], round(n["x"], 4), round(n["y"], 4))
                            for n in data["nodes"]]
check_true("force and radial differ", arrangements["force"] != arrangements["radial"])
check_true("radial and mind map differ", arrangements["radial"] != arrangements["mindmap"])
check_true("force and mind map differ", arrangements["force"] != arrangements["mindmap"])

# -- the mind map groups each category into its own band ---------------------
mindmap = {n["id"]: n for n in knowledge.get_graph_data("mindmap")["nodes"]}
bands = {}
for node in mindmap.values():
    bands.setdefault(node["category"], []).append(node["y"])
spans = {category: max(ys) - min(ys) for category, ys in bands.items()}
check_true("each category occupies a narrow band in the mind map",
           all(span < 0.35 for span in spans.values()))
centres = sorted((sum(ys) / len(ys), category) for category, ys in bands.items())
check_true("the bands are separated from one another",
           all(centres[i + 1][0] - centres[i][0] > 0.15 for i in range(len(centres) - 1)))

# -- edges link facts within a category, not across ---------------------------
data = knowledge.get_graph_data("force")
category_of = {n["id"]: n["category"] for n in data["nodes"]}
check_true("edges only ever join facts in the same category",
           all(category_of[e["source"]] == category_of[e["target"]] for e in data["edges"]))
check_true("facts in a category are actually linked", len(data["edges"]) > 0)

# -- an unknown layout name falls back rather than failing --------------------
fallback = knowledge.get_graph_data("no-such-layout")
check("an unknown layout name falls back to the force layout",
      [(n["id"], round(n["x"], 6)) for n in fallback["nodes"]],
      [(n["id"], round(n["x"], 6)) for n in knowledge.get_graph_data("force")["nodes"]])

if failures:
    print(f"{len(failures)} PROBLEM(S):")
    for f in failures:
        print("  ", f)
    sys.exit(1)
print("memory graph layout checks passed")
