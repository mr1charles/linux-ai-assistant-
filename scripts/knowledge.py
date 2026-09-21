"""
knowledge.py — a local, never-fabricated knowledge graph about the user.

Facts are only ever added when the user explicitly states them. The
extraction prompt is deliberately restrictive: no inference, no guessing.
If information is incomplete, it stays incomplete rather than being filled in.

Storage: a flat JSON file, one entry per fact:
  {"id": "f0001", "text": "...", "category": "...", "added": "2026-08-16T..."}

Two renders:
  render_tree()   -> category -> fact hierarchy, broad to detailed
  render_bubbles() -> facts clustered by category, connected within cluster

Both use matplotlib (no pygraphviz dependency — a simple manual layout).
"""

import json
import subprocess
from datetime import datetime
from pathlib import Path

import requests

KNOWLEDGE_PATH = Path.home() / "linux-agent" / "knowledge.json"
OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "qwen2.5:7b-instruct"

EXTRACT_PROMPT = """You extract facts a user has explicitly stated about themselves.

STRICT RULES:
- Only include something if the user directly and explicitly said it about themselves.
- NEVER infer, assume, or guess. If it's not explicitly stated, don't include it.
- Do not extract facts about other people, only about the user.
- Do not extract requests, commands, or questions — only self-descriptive statements.
- If there is nothing new and explicit, return an empty list.

Respond with ONLY a JSON array, no prose, no markdown fences. Each item:
{"text": "short factual statement", "category": "one or two word category"}

Example input: "I play soccer on weekends and I hate cilantro"
Example output: [{"text": "Plays soccer on weekends", "category": "hobbies"}, {"text": "Dislikes cilantro", "category": "food"}]

Example input: "can you open youtube"
Example output: []
"""


def _load():
    if not KNOWLEDGE_PATH.exists():
        return {"facts": []}
    try:
        return json.loads(KNOWLEDGE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {"facts": []}


def _save(data):
    KNOWLEDGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    KNOWLEDGE_PATH.write_text(json.dumps(data, indent=2))


def _next_id(data):
    n = len(data["facts"]) + 1
    return f"f{n:04d}"


def extract_and_store(user_message: str) -> list:
    """Runs a dedicated, restrictive extraction pass. Returns newly added facts."""
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": EXTRACT_PROMPT},
            {"role": "user", "content": user_message},
        ],
        "stream": False,
        "format": "json",
    }
    try:
        resp = requests.post(OLLAMA_URL, json=payload, timeout=30)
        resp.raise_for_status()
        raw = resp.json()["message"]["content"].strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        candidates = json.loads(raw)
        if isinstance(candidates, dict):
            candidates = [candidates]
        if not isinstance(candidates, list):
            return []
    except Exception:
        return []

    data = _load()
    existing_texts = {f["text"].lower().strip() for f in data["facts"]}
    added = []

    for c in candidates:
        text = c.get("text", "").strip()
        category = c.get("category", "general").strip().lower() or "general"
        if not text or text.lower() in existing_texts:
            continue
        fact = {
            "id": _next_id(data),
            "text": text,
            "category": category,
            "added": datetime.now().isoformat(timespec="seconds"),
        }
        data["facts"].append(fact)
        existing_texts.add(text.lower())
        added.append(fact)

    if added:
        _save(data)
    return added


def get_facts_summary(max_facts=30) -> str:
    """Short text summary for injecting into the main conversation prompt as context."""
    data = _load()
    facts = data["facts"][-max_facts:]
    if not facts:
        return ""
    lines = [f"- [{f['category']}] {f['text']}" for f in facts]
    return "Known facts about the user (only use these if relevant, never invent more):\n" + "\n".join(lines)


def _open_image(path: Path):
    subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def render_tree() -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    data = _load()
    if not data["facts"]:
        return "No facts recorded yet — nothing to show."

    categories = {}
    for f in data["facts"]:
        categories.setdefault(f["category"], []).append(f["text"])

    fig, ax = plt.subplots(figsize=(14, 8))
    ax.axis("off")

    root_x, root_y = 0.5, 0.95
    ax.text(root_x, root_y, "YOU", ha="center", va="center", fontsize=16,
             fontweight="bold", bbox=dict(boxstyle="round,pad=0.5", fc="#4a4a6a", ec="none"), color="white")

    n_cats = len(categories)
    cat_positions = {}
    for i, cat in enumerate(categories):
        x = (i + 0.5) / n_cats
        y = 0.65
        cat_positions[cat] = (x, y)
        ax.plot([root_x, x], [root_y - 0.03, y + 0.04], color="#888", lw=1.5, zorder=1)
        ax.text(x, y, cat, ha="center", va="center", fontsize=12, fontweight="bold",
                 bbox=dict(boxstyle="round,pad=0.4", fc="#6a8caf", ec="none"), color="white", zorder=2)

    for cat, facts in categories.items():
        cx, cy = cat_positions[cat]
        n = len(facts)
        for j, fact_text in enumerate(facts):
            x = cx + (j - (n - 1) / 2) * min(0.15, 0.5 / max(n, 1))
            y = cy - 0.25 - (j % 3) * 0.08
            ax.plot([cx, x], [cy - 0.03, y + 0.03], color="#bbb", lw=1, zorder=1)
            wrapped = fact_text if len(fact_text) < 28 else fact_text[:25] + "..."
            ax.text(x, y, wrapped, ha="center", va="center", fontsize=9,
                     bbox=dict(boxstyle="round,pad=0.3", fc="#f0f0f0", ec="#999"), zorder=2)

    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    plt.tight_layout()

    out_path = KNOWLEDGE_PATH.parent / "knowledge_tree.png"
    plt.savefig(out_path, dpi=130)
    plt.close(fig)
    _open_image(out_path)
    return f"Tree rendered to {out_path}"


def render_bubbles() -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import networkx as nx

    data = _load()
    if not data["facts"]:
        return "No facts recorded yet — nothing to show."

    G = nx.Graph()
    for f in data["facts"]:
        G.add_node(f["id"], label=f["text"], category=f["category"])

    by_category = {}
    for f in data["facts"]:
        by_category.setdefault(f["category"], []).append(f["id"])
    for ids in by_category.values():
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                G.add_edge(ids[i], ids[j])

    pos = nx.spring_layout(G, k=0.9, seed=42)

    categories = sorted(by_category.keys())
    cmap = plt.get_cmap("tab20")
    color_map = {cat: cmap(i / max(len(categories), 1)) for i, cat in enumerate(categories)}
    node_colors = [color_map[G.nodes[n]["category"]] for n in G.nodes]
    node_sizes = [400 + 200 * len(by_category[G.nodes[n]["category"]]) for n in G.nodes]

    fig, ax = plt.subplots(figsize=(14, 10))
    nx.draw_networkx_edges(G, pos, alpha=0.3, ax=ax)
    nx.draw_networkx_nodes(G, pos, node_color=node_colors, node_size=node_sizes, alpha=0.9, ax=ax)

    labels = {n: (G.nodes[n]["label"][:20] + "..." if len(G.nodes[n]["label"]) > 20 else G.nodes[n]["label"])
              for n in G.nodes}
    nx.draw_networkx_labels(G, pos, labels, font_size=7, ax=ax)

    ax.axis("off")
    plt.tight_layout()

    out_path = KNOWLEDGE_PATH.parent / "knowledge_bubbles.png"
    plt.savefig(out_path, dpi=130)
    plt.close(fig)
    _open_image(out_path)
    return f"Bubble graph rendered to {out_path}"


# ---------------------------------------------------------------------------
# Data access for the in-app interactive graph view (Memories page). Unlike
# render_tree/render_bubbles above, these never touch matplotlib or open a
# file — they hand back plain data + a computed layout for Toby's own Cairo
# widget to draw, pan, zoom, and let the user edit directly.
# ---------------------------------------------------------------------------

def get_graph_data() -> dict:
    """Returns {"nodes": [{id, label, category, x, y}], "edges": [{source, target}]}
    with x/y already normalized to 0..1. Empty lists if there's nothing yet."""
    data = _load()
    if not data["facts"]:
        return {"nodes": [], "edges": []}

    import networkx as nx

    G = nx.Graph()
    for f in data["facts"]:
        G.add_node(f["id"], label=f["text"], category=f["category"])

    by_category = {}
    for f in data["facts"]:
        by_category.setdefault(f["category"], []).append(f["id"])
    for ids in by_category.values():
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                G.add_edge(ids[i], ids[j])

    pos = nx.spring_layout(G, k=0.9, seed=42) if G.number_of_nodes() > 1 else {
        n: (0.5, 0.5) for n in G.nodes
    }

    xs = [p[0] for p in pos.values()] or [0.0]
    ys = [p[1] for p in pos.values()] or [0.0]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    spanx = (maxx - minx) or 1.0
    spany = (maxy - miny) or 1.0

    nodes = []
    for n in G.nodes:
        x, y = pos[n]
        nodes.append({
            "id": n,
            "label": G.nodes[n]["label"],
            "category": G.nodes[n]["category"],
            "x": (x - minx) / spanx,
            "y": (y - miny) / spany,
        })
    edges = [{"source": u, "target": v} for u, v in G.edges()]
    return {"nodes": nodes, "edges": edges}


def delete_fact(fact_id: str) -> bool:
    data = _load()
    before = len(data["facts"])
    data["facts"] = [f for f in data["facts"] if f["id"] != fact_id]
    if len(data["facts"]) != before:
        _save(data)
        return True
    return False


def add_fact_manual(text: str, category: str) -> dict | None:
    """User-initiated add from the Memories UI (as opposed to extract_and_store,
    which only ever adds facts the user stated in conversation). Still de-dupes."""
    text = text.strip()
    category = (category.strip().lower() or "general")
    if not text:
        return None
    data = _load()
    existing_texts = {f["text"].lower().strip() for f in data["facts"]}
    if text.lower() in existing_texts:
        return None
    fact = {
        "id": _next_id(data),
        "text": text,
        "category": category,
        "added": datetime.now().isoformat(timespec="seconds"),
    }
    data["facts"].append(fact)
    _save(data)
    return fact


def get_categories() -> list:
    data = _load()
    return sorted({f["category"] for f in data["facts"]})
