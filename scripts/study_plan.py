"""
study_plan.py — the "real study tool" layer on top of study_notes.py.

Handles:
  - ingesting uploaded material (PDF/text files) and having the model pull
    out the subject + topics covered (or, if it's a graded assignment,
    just the subject area — never the answers, same line study_notes.py
    already draws)
  - tracking a proficiency score per topic, built from real quiz results
    (not guessed, not self-reported — it only moves when you actually take
    a quiz on that topic)
  - handing back resource links for a topic

On that last point: Toby has no web search tool, so this deliberately never
asks the model to name a specific video/article — that's how you get a
confident-sounding link to something that doesn't exist. Instead it builds
plain search-engine URLs (YouTube, Khan Academy, Wikipedia) for the topic,
which are always valid and let the student pick the real result themselves.
"""

import json
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

PLAN_PATH = Path.home() / "linux-agent" / "study_plan.json"


def _load():
    if PLAN_PATH.exists():
        try:
            data = json.loads(PLAN_PATH.read_text())
            data.setdefault("topics", [])
            data.setdefault("materials", [])
            return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"topics": [], "materials": []}


def _save(data):
    PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLAN_PATH.write_text(json.dumps(data, indent=2))


def _next_id(items):
    return str(max([int(x["id"]) for x in items], default=0) + 1)


# ---------------------------------------------------------------------------
# Ingesting uploaded material
# ---------------------------------------------------------------------------

def extract_text_from_file(filepath: str) -> str:
    path = Path(filepath)
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    return path.read_text(errors="ignore")


INGEST_PROMPT = """A student uploaded study material. You're given raw extracted text from
it (a textbook chapter, notes, or an assignment). Respond with ONLY a JSON object, no prose,
no markdown fences:
{"subject": "...", "is_assignment": true|false, "topics": ["...", "..."], "summary": "..."}

STRICT RULES:
- "subject" is a short category like "Algebra", "US History", "Cell Biology".
- "topics" is 2-6 short specific topic names actually covered in the text (e.g. "Quadratic
  Formula", "The Treaty of Versailles"), suitable as labels on a study-tracking board.
- If this looks like a graded homework/quiz/exam question set the student is meant to answer
  for submission, set "is_assignment": true. Still fill in "subject" and general "topics" the
  assignment is ABOUT (for planning purposes) — but "summary" must not answer any question in
  it, just describe what it's about in one line.
- If not an assignment, "summary" is a few sentences a student could use as a study overview.
"""


def ingest_material(filepath: str, ollama_url: str, ollama_model: str) -> dict:
    import requests

    try:
        text = extract_text_from_file(filepath)
    except Exception as e:
        return {"error": f"Couldn't read that file: {e}"}
    if not text.strip():
        return {"error": "Couldn't find any text in that file."}

    payload = {
        "model": ollama_model,
        "messages": [
            {"role": "system", "content": INGEST_PROMPT},
            {"role": "user", "content": text[:6000]},
        ],
        "stream": False,
        "format": "json",
    }
    try:
        resp = requests.post(ollama_url, json=payload, timeout=180)
        resp.raise_for_status()
        raw = resp.json()["message"]["content"].strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        result = json.loads(raw)
    except Exception as e:
        return {"error": f"Couldn't analyze that file: {e}"}

    result.setdefault("subject", "General")
    result.setdefault("is_assignment", False)
    result.setdefault("topics", [])
    result.setdefault("summary", "")

    data = _load()
    material = {
        "id": _next_id(data["materials"]),
        "filename": Path(filepath).name,
        "subject": result["subject"],
        "topics": result["topics"],
        "is_assignment": result["is_assignment"],
        "summary": result["summary"],
        "uploaded_at": datetime.now().isoformat(timespec="seconds"),
    }
    data["materials"].append(material)
    for topic_name in result["topics"]:
        _ensure_topic(data, result["subject"], topic_name, source=material["filename"])
    _save(data)
    result["saved"] = True
    return result


def _ensure_topic(data, subject, topic_name, source=""):
    for t in data["topics"]:
        if t["subject"] == subject and t["topic"].lower() == topic_name.lower():
            if source and source not in t.get("sources", []):
                t.setdefault("sources", []).append(source)
            return t
    topic = {
        "id": _next_id(data["topics"]),
        "subject": subject,
        "topic": topic_name,
        "proficiency": None,  # None = not yet assessed, else 0-100
        "quiz_count": 0,
        "last_assessed": None,
        "sources": [source] if source else [],
    }
    data["topics"].append(topic)
    return topic


# ---------------------------------------------------------------------------
# Proficiency tracking — only moves when a quiz is actually taken
# ---------------------------------------------------------------------------

def record_quiz_result(subject: str, topic_name: str, score_fraction: float):
    """score_fraction: 0.0-1.0, e.g. 4/5 correct -> 0.8"""
    data = _load()
    topic = _ensure_topic(data, subject, topic_name)
    new_score = round(score_fraction * 100)
    if topic["proficiency"] is None:
        topic["proficiency"] = new_score
    else:
        # weighted average — recent performance matters more, but one bad
        # quiz after a string of good ones shouldn't erase the history
        topic["proficiency"] = round(topic["proficiency"] * 0.6 + new_score * 0.4)
    topic["quiz_count"] += 1
    topic["last_assessed"] = datetime.now().isoformat(timespec="seconds")
    _save(data)
    return topic


def get_all_topics() -> list:
    return _load()["topics"]


def get_topic(topic_id: str):
    for t in _load()["topics"]:
        if t["id"] == topic_id:
            return t
    return None


def delete_topic(topic_id: str) -> bool:
    data = _load()
    before = len(data["topics"])
    data["topics"] = [t for t in data["topics"] if t["id"] != topic_id]
    if len(data["topics"]) != before:
        _save(data)
        return True
    return False


def get_materials() -> list:
    return _load()["materials"]


# ---------------------------------------------------------------------------
# Resource links — always valid search URLs, never a guessed specific link
# ---------------------------------------------------------------------------

def get_resource_links(subject: str, topic_name: str) -> list:
    query = f"{subject} {topic_name}".strip()
    q = quote_plus(query)
    return [
        {"label": "Search YouTube", "url": f"https://www.youtube.com/results?search_query={q}"},
        {"label": "Search Khan Academy", "url": f"https://www.khanacademy.org/search?page_search_query={q}"},
        {"label": "Search Wikipedia", "url": f"https://en.wikipedia.org/w/index.php?search={q}"},
    ]
