"""
study_notes.py — organized academic notes, saved explicitly (never via passive
background screen monitoring — that path was deliberately not built, to avoid
this sliding into homework-harvesting/auto-completion territory).

Notes get added when the user explicitly asks ("add this to my ELA notes: ...")
or during a genuine study/review conversation. They're then injected into
context so the model can naturally quiz, summarize, or make flashcards from
real material — same mechanism as the personal-facts knowledge graph.
"""

import json
from datetime import datetime
from pathlib import Path

NOTES_PATH = Path.home() / "linux-agent" / "study_notes.json"


def _load():
    if not NOTES_PATH.exists():
        return {"notes": []}
    try:
        return json.loads(NOTES_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {"notes": []}


def _save(data):
    NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
    NOTES_PATH.write_text(json.dumps(data, indent=2))


def add_note(subject: str, topic: str, content: str) -> str:
    data = _load()
    note = {
        "id": f"n{len(data['notes']) + 1:04d}",
        "subject": subject.strip(),
        "topic": topic.strip(),
        "content": content.strip(),
        "added": datetime.now().isoformat(timespec="seconds"),
    }
    data["notes"].append(note)
    _save(data)
    return f"Saved to {subject} → {topic}."


def get_notes(subject: str = None) -> list:
    data = _load()
    if subject:
        subject_lower = subject.strip().lower()
        return [n for n in data["notes"] if n["subject"].lower() == subject_lower]
    return data["notes"]


def get_subjects() -> list:
    data = _load()
    return sorted({n["subject"] for n in data["notes"]})


def get_notes_summary(max_notes=40) -> str:
    """Injected into conversation context, same pattern as knowledge.get_facts_summary()."""
    data = _load()
    notes = data["notes"][-max_notes:]
    if not notes:
        return ""
    by_subject = {}
    for n in notes:
        by_subject.setdefault(n["subject"], []).append(n)
    lines = ["Study notes on file (use these for quizzing/summarizing/flashcards when asked):"]
    for subject, items in by_subject.items():
        lines.append(f"\n{subject}:")
        for n in items:
            lines.append(f"  - [{n['topic']}] {n['content']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Studying Mode helper actions — all user-initiated (a button click), never a
# silent background poll. This is a deliberate line: summarizing/quizzing real
# study material the user is actively looking at is useful; silently OCRing
# whatever's on screen on a timer and auto-generating content is not something
# this module does, since screen content while studying is very often a
# homework/assignment prompt, and an AI that quietly reads and "helps" with
# that on its own initiative is a fast path to writing answers no one asked
# it to write. Same reasoning as extract_and_store's restriction above —
# applied here as "requires a click" rather than "requires a chat message".
# ---------------------------------------------------------------------------

SUMMARIZE_PROMPT = """You are helping a student study from something on their screen. You will
be given raw OCR text captured from that screen. Respond with ONLY a JSON object, no prose,
no markdown fences:
{"material_type": "book|article|lecture_notes|presentation|code|assignment|other",
 "is_assignment": true|false,
 "summary": "...",
 "difficult_concepts": [{"concept": "...", "explanation": "..."}]}

STRICT RULES:
- If the text looks like a graded homework/quiz/exam question set (numbered questions the
  student is meant to answer for submission), set "is_assignment": true, leave "summary" as
  a single short line ("This looks like an assignment — ask about the underlying concepts
  rather than the answers."), and leave "difficult_concepts" empty. Do NOT answer the
  questions, even partially.
- Otherwise ("is_assignment": false), write a clear, genuinely useful study summary (a
  paragraph or a few bullet points condensed into one string), and list 0-3 concepts in the
  text a student might find difficult, each with a short, clear explanation.
- If the OCR text is too garbled or short to make sense of, set material_type to "other" and
  summary to a one-line note saying so, with empty difficult_concepts.
"""


def summarize_material(text: str, source_label: str = "Screen capture") -> dict:
    """User clicked "Summarize Screen" in the Study Helper. Captures once, summarizes
    once — no polling. Auto-saves the summary (and any difficult-concept explanations)
    as notes, unless the material looks like a graded assignment, in which case nothing
    is saved and the caller should just show the "looks like an assignment" note."""
    import requests

    payload = {
        "model": "qwen2.5:7b-instruct",
        "messages": [
            {"role": "system", "content": SUMMARIZE_PROMPT},
            {"role": "user", "content": text[:4000]},
        ],
        "stream": False,
        "format": "json",
    }
    try:
        resp = requests.post("http://localhost:11434/api/chat", json=payload, timeout=60)
        resp.raise_for_status()
        raw = resp.json()["message"]["content"].strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        result = json.loads(raw)
    except Exception as e:
        return {"material_type": "other", "is_assignment": False,
                "summary": f"Couldn't summarize ({e}).", "difficult_concepts": [], "saved": False}

    result.setdefault("material_type", "other")
    result.setdefault("is_assignment", False)
    result.setdefault("summary", "")
    result.setdefault("difficult_concepts", [])

    if not result["is_assignment"] and result["summary"]:
        add_note(result["material_type"].replace("_", " ").title(), source_label, result["summary"])
        for concept in result["difficult_concepts"][:3]:
            c = concept.get("concept", "").strip()
            e = concept.get("explanation", "").strip()
            if c and e:
                add_note(result["material_type"].replace("_", " ").title(), f"{source_label} — {c}", e)
        result["saved"] = True
    else:
        result["saved"] = False
    return result


QUIZ_PROMPT = """You write multiple-choice quiz questions strictly from the study notes given
to you — never invent facts that aren't in the notes. Respond with ONLY a JSON array, no
prose, no markdown fences, of up to {count} items:
[{"question": "...", "options": ["...", "...", "...", "..."], "answer_index": 0}]
Each question needs exactly 4 options with answer_index pointing at the correct one (0-3).
If the notes don't have enough distinct material for {count} good questions, return fewer —
never pad with filler or repeat the same fact reworded."""

FLASHCARD_PROMPT = """You turn study notes into flashcards — strictly from the notes given to
you, never invented. Respond with ONLY a JSON array, no prose, no markdown fences, of up to
{count} items: [{"front": "a question or term", "back": "the answer or definition"}]
If the notes don't support {count} good distinct cards, return fewer."""


def generate_quiz(count=5, subject=None) -> list:
    return _generate_from_notes(QUIZ_PROMPT, count, subject)


def generate_flashcards(count=8, subject=None) -> list:
    return _generate_from_notes(FLASHCARD_PROMPT, count, subject)


def _generate_from_notes(prompt_template, count, subject):
    import requests

    notes = get_notes(subject)
    if not notes:
        return []
    notes_text = "\n".join(f"[{n['subject']}] {n['topic']}: {n['content']}" for n in notes[-60:])
    payload = {
        "model": "qwen2.5:7b-instruct",
        "messages": [
            {"role": "system", "content": prompt_template.format(count=count)},
            {"role": "user", "content": notes_text},
        ],
        "stream": False,
        "format": "json",
    }
    try:
        resp = requests.post("http://localhost:11434/api/chat", json=payload, timeout=60)
        resp.raise_for_status()
        raw = resp.json()["message"]["content"].strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        items = json.loads(raw)
        if isinstance(items, dict):
            items = [items]
        return items if isinstance(items, list) else []
    except Exception:
        return []


EXTRACT_NOTE_PROMPT = """You extract genuine academic/study concepts from a conversation
message — things being discussed, learned, or reviewed. This is for building study notes,
NOT for capturing homework answers.

STRICT RULES:
- Only extract if the message is clearly discussing academic material (a concept, a theme,
  a fact about a subject, a summary of something read/learned).
- NEVER extract a homework answer, essay paragraph, or anything written to be submitted
  as an assignment response. If the message reads like a completed assignment answer
  rather than a study concept, return an empty list.
- Do not invent a subject/topic if it's unclear — skip it instead.
- If there is nothing clearly academic, return an empty list.

Respond with ONLY a JSON array, no prose, no markdown fences. Each item:
{"subject": "ELA", "topic": "short topic", "content": "the concept, in a sentence or two"}

Example input: "we're covering photosynthesis in bio, plants convert light into energy using chlorophyll"
Example output: [{"subject": "Biology", "topic": "Photosynthesis", "content": "Plants convert light into energy using chlorophyll"}]

Example input: "can you open youtube"
Example output: []
"""


def extract_and_store(user_message: str, screen_context: str = "") -> list:
    """Same restrictive extraction pattern as knowledge.extract_and_store, scoped to
    academic concepts rather than personal facts. screen_context, if provided, is a
    recent OCR snapshot that helps identify what book/document is currently being
    read, so notes get tagged with the right subject/topic automatically."""
    import requests

    context_note = ""
    if screen_context:
        context_note = (
            "\n\nFor extra context, here is text recently read from the user's screen "
            "(use it only to help identify what book/document/subject this relates to, "
            "never treat it as something to extract on its own):\n"
            + screen_context[:1000]
        )

    payload = {
        "model": "qwen2.5:7b-instruct",
        "messages": [
            {"role": "system", "content": EXTRACT_NOTE_PROMPT + context_note},
            {"role": "user", "content": user_message},
        ],
        "stream": False,
        "format": "json",
    }
    try:
        resp = requests.post("http://localhost:11434/api/chat", json=payload, timeout=30)
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

    added = []
    for c in candidates:
        subject = c.get("subject", "").strip()
        topic = c.get("topic", "").strip()
        content = c.get("content", "").strip()
        if not (subject and topic and content):
            continue
        add_note(subject, topic, content)
        added.append(c)
    return added
