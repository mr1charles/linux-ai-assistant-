"""
notes.py — your own Markdown notes as Toby's memory.

Point Toby at a folder of Markdown notes (an Obsidian vault, or any folder;
`toby notes set ~/Obsidian`) and it will:

- search them when a question might be answered there, and put the few most
  relevant passages in front of the model, so "what did my notes say about
  the dentist?" is answered from your notes rather than guessed;
- keep what it learns about you in <folder>/Toby/Memory.md, a plain list you
  can read, edit and delete from in your notes app. That file *is* Toby's
  memory: the Memories graph shows it, and a line you delete is forgotten;
- write a short daily log of what it did in <folder>/Toby/Log/<date>.md, and
  notes you ask for in <folder>/Toby/Notes/.

Nothing leaves the computer: the search is a local full-text index (SQLite's
FTS5, ranked with BM25) in ~/linux-agent/notes_index.sqlite, readable by you
alone. It needs no extra packages or models, and only notes that changed
are read again.

What's never read
-----------------
Hidden folders (.obsidian, .trash, .git), anything whose folder name
suggests it's private (Private, Passwords, 2FA, Secrets, Finance, Health,
Medical), and anything matched by a .tobyignore file in the folder (one
pattern per line, like .gitignore: `Journal/`, `*.secret.md`). Text that
looks like a key or a password (sk-…, ghp_…, "password: …", private key
blocks) is blanked out of anything shown to the model.
"""

import fnmatch
import hashlib
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

INDEX_PATH = Path.home() / "linux-agent" / "notes_index.sqlite"
TOBY_DIR = "Toby"
MEMORY_FILE = "Memory.md"
NOTE_SUFFIXES = {".md", ".markdown", ".txt"}
MAX_FILE_BYTES = 1_000_000
MAX_FILES = 20_000
CHUNK_CHARS = 700
RESCAN_EVERY_S = 60
INDEX_VERSION = 3

PRIVATE_WORDS = ("private", "password", "passwords", "2fa", "secret", "secrets", "finance",
                 "finances", "health", "medical", "credentials")

STOPWORDS = set("""a an and are as at be but by can could did do does for from had has have he her
him his how i if in into is it its just me my of on or our so than that the their them then there
these they this to too up us was we were what when where which who why will with would you your
about all any been being did does doing each few more most no nor not only other own same should
some such very""".split())

_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(ghp|gho|ghs|ghu|github_pat)_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abpors]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"),                     # bot tokens
    re.compile(r"(?i)\b(password|passcode|passwd|pin|api[_ -]?key|secret|token)\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(password|passcode|passwd|pin code|api key)\s+is\s+\S+"),
]


def redact(text):
    """Blank out anything that looks like a key or a password."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[hidden]", text)
    return text


def folder(settings):
    """The notes folder from settings, if it's set and exists."""
    raw = str((settings or {}).get("notes_folder") or "").strip()
    if not raw:
        return None
    path = Path(os.path.expanduser(raw))
    return path if path.is_dir() else None


# ---------------------------------------------------------------------------
# What to read
# ---------------------------------------------------------------------------

def ignore_patterns(root):
    """Patterns from <root>/.tobyignore, like .gitignore (no negation)."""
    try:
        lines = (Path(root) / ".tobyignore").read_text().splitlines()
    except OSError:
        return []
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def excluded(rel, patterns=()):
    """Whether a path (relative to the notes folder) is off limits."""
    parts = Path(rel).parts
    if parts[:2] == (TOBY_DIR, "Log") or parts in ((TOBY_DIR, MEMORY_FILE), (TOBY_DIR, "Queue.md")):
        return True     # Toby's own bookkeeping: already in its context, or not worth searching
    for part in parts[:-1]:
        low = part.lower()
        if part.startswith("."):
            return True
        if any(low == w or low.startswith(w + " ") or low.startswith(w + "-") or low.startswith(w + "_")
               or low.endswith(" " + w) or low.endswith("-" + w) for w in PRIVATE_WORDS):
            return True
    if parts and parts[-1].startswith("."):
        return True
    text = "/".join(parts)
    for pattern in patterns:
        p = pattern.strip("/")
        if pattern.endswith("/"):
            if any(fnmatch.fnmatch(part, p) for part in parts[:-1]) or fnmatch.fnmatch(text, p + "/*"):
                return True
        elif fnmatch.fnmatch(text, p) or fnmatch.fnmatch(parts[-1], p):
            return True
    return False


def note_files(root, patterns=None):
    """Every note Toby may read, as (relative path, full path)."""
    root = Path(root)
    patterns = ignore_patterns(root) if patterns is None else patterns
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames[:] = sorted(d for d in dirnames if not excluded(rel_dir / d / "x", patterns))
        for name in sorted(filenames):
            rel = rel_dir / name
            if Path(name).suffix.lower() in NOTE_SUFFIXES and not excluded(rel, patterns):
                found.append((str(rel), Path(dirpath) / name))
                if len(found) >= MAX_FILES:
                    return found
    return found


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def stem(word):
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def tokens(text):
    return [stem(w) for w in (m.group(0).lower() for m in _WORD.finditer(text))
            if w not in STOPWORDS and len(w) > 1]


def chunk_markdown(text):
    """Split a note into (heading, passage) pieces of a readable size."""
    pieces, heading, buf = [], "", []

    def flush():
        body = "\n".join(buf).strip()
        while body:
            if len(body) <= CHUNK_CHARS:
                pieces.append((heading, body))
                break
            cut = body.rfind("\n", 0, CHUNK_CHARS)
            cut = cut if cut > CHUNK_CHARS // 3 else body.rfind(" ", 0, CHUNK_CHARS)
            cut = cut if cut > 0 else CHUNK_CHARS
            pieces.append((heading, body[:cut].strip()))
            body = body[cut:].strip()

    in_front_matter = text.startswith("---\n")
    for i, line in enumerate(text.splitlines()):
        if in_front_matter:
            if i > 0 and line.strip() == "---":
                in_front_matter = False
            continue
        match = re.match(r"^(#{1,6})\s+(.*)", line)
        if match:
            flush()
            buf = []
            heading = match.group(2).strip()
        else:
            buf.append(line)
    flush()
    return pieces


class NotesIndex:
    """A full-text index of the notes folder (SQLite FTS5, BM25 ranking),
    kept on disk and brought up to date as notes change."""

    def __init__(self, root, index_path=None, clock=time.time):
        self.root = Path(root)
        self.index_path = Path(index_path or INDEX_PATH)
        self.clock = clock
        self._lock = threading.RLock()
        self.last_scan = 0.0
        self.skipped = 0
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.index_path.exists():           # it holds passages of your notes: yours alone
            os.close(os.open(self.index_path, os.O_WRONLY | os.O_CREAT, 0o600))
        self.db = sqlite3.connect(str(self.index_path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self._schema()

    def _schema(self):
        with self._lock, self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
            row = self.db.execute("SELECT value FROM meta WHERE key='root'").fetchone()
            version = self.db.execute("SELECT value FROM meta WHERE key='version'").fetchone()
            if row is None or row[0] != str(self.root) or version is None or version[0] != str(INDEX_VERSION):
                self.db.execute("DROP TABLE IF EXISTS files")
                self.db.execute("DROP TABLE IF EXISTS passages")
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('root', ?)", (str(self.root),))
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('version', ?)", (str(INDEX_VERSION),))
            self.db.execute("CREATE TABLE IF NOT EXISTS files(rel TEXT PRIMARY KEY, mtime REAL, size INTEGER, "
                            "rowids TEXT)")
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS passages USING fts5("
                            "rel UNINDEXED, title, heading, body, tokenize='porter unicode61')")

    def close(self):
        with self._lock:
            self.db.close()

    # -- keeping up to date ---------------------------------------------------------------
    def refresh(self, force=False):
        """Re-read notes that changed since last time. Returns how many did."""
        with self._lock:
            if not force and self.clock() - self.last_scan < RESCAN_EVERY_S:
                return 0
            self.last_scan = self.clock()
            known, rows = {}, {}
            for rel, mtime, size, rowids in self.db.execute("SELECT rel, mtime, size, rowids FROM files"):
                known[rel] = (mtime, size)
                rows[rel] = [int(r) for r in (rowids or "").split(",") if r]
            seen, changed, skipped = set(), 0, 0

            def forget(rel):
                # by rowid: the path column isn't indexed, so matching on it would scan everything
                self.db.executemany("DELETE FROM passages WHERE rowid = ?", [(r,) for r in rows.get(rel, [])])

            with self.db:
                for rel, path in note_files(self.root):
                    try:
                        st = path.stat()
                    except OSError:
                        continue
                    seen.add(rel)
                    if known.get(rel) == (st.st_mtime, st.st_size):
                        continue
                    if st.st_size > MAX_FILE_BYTES:
                        skipped += 1
                        continue
                    try:
                        text = path.read_text(errors="replace")
                    except OSError:
                        continue
                    forget(rel)
                    title = Path(rel).stem
                    new_rows = [self.db.execute("INSERT INTO passages(rel, title, heading, body) VALUES (?, ?, ?, ?)",
                                                (rel, title, heading, body)).lastrowid
                                for heading, body in chunk_markdown(text)]
                    self.db.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?)",
                                    (rel, st.st_mtime, st.st_size, ",".join(map(str, new_rows))))
                    changed += 1
                gone = [rel for rel in known if rel not in seen]
                for rel in gone:
                    forget(rel)
                    self.db.execute("DELETE FROM files WHERE rel = ?", (rel,))
            self.skipped = skipped
            return changed + len(gone)

    # -- asking -------------------------------------------------------------------------------
    def search(self, query, limit=5):
        """The passages that best match, best first."""
        self.refresh()
        terms = list(dict.fromkeys(tokens(query)))
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms)
        with self._lock:
            total = self.db.execute("SELECT count(*) FROM passages").fetchone()[0]
            if not total:
                return []
            # A word found in most passages says little about which one you mean
            # (only judged once there are enough passages to tell).
            telling = set()
            for t in terms:
                df = self.db.execute("SELECT count(*) FROM passages WHERE passages MATCH ?", (f'"{t}"',)).fetchone()[0]
                if total < 20 or df / total <= 0.3:
                    telling.add(t)
            rows = self.db.execute(
                "SELECT rel, heading, body, bm25(passages, 0.0, 3.0, 2.0, 1.0) AS rank FROM passages "
                "WHERE passages MATCH ? ORDER BY rank LIMIT ?", (match, limit)).fetchall()
        out = []
        for rel, heading, body, rank in rows:
            have = set(tokens(f"{Path(rel).stem} {heading} {body}"))
            matched = {t for t in terms if t in have}
            out.append({"path": str(self.root / rel), "rel": rel, "title": Path(rel).stem, "heading": heading,
                        "snippet": redact(snippet(body, terms)), "score": round(-rank, 3),
                        "coverage": round(len(matched) / len(terms), 2), "telling": bool(matched & telling)})
        return out

    def notes(self):
        """The notes in the index, by path relative to the folder."""
        with self._lock:
            return sorted(r[0] for r in self.db.execute("SELECT rel FROM files"))

    def passages_of(self, rel):
        with self._lock:
            row = self.db.execute("SELECT rowids FROM files WHERE rel = ?", (rel,)).fetchone()
            ids = [int(r) for r in ((row or [""])[0] or "").split(",") if r]
            return [{"heading": h, "text": b} for h, b in
                    (self.db.execute("SELECT heading, body FROM passages WHERE rowid = ?", (i,)).fetchone()
                     for i in ids)]

    def stats(self):
        with self._lock:
            files = self.db.execute("SELECT count(*) FROM files").fetchone()[0]
            passages = self.db.execute("SELECT count(*) FROM passages").fetchone()[0]
        return {"files": files, "passages": passages, "too_big": self.skipped}


def snippet(text, terms, width=320):
    """The part of a passage around its best-matching words."""
    flat = re.sub(r"\s+", " ", text).strip()
    if len(flat) <= width:
        return flat
    low = flat.lower()
    best, best_hits = 0, -1
    for start in range(0, max(1, len(flat) - width + 1), 40):
        window = low[start:start + width]
        hits = sum(window.count(t) for t in terms)
        if hits > best_hits:
            best, best_hits = start, hits
    piece = flat[best:best + width]
    return ("…" if best else "") + piece + ("…" if best + width < len(flat) else "")


def context_for(index, question, budget=1200, limit=3):
    """The passages worth putting in front of the model for this question, as
    a block of text within budget, or "" when nothing matches well enough:
    a passage has to cover most of a short question's words (half of a long
    one's), including one that isn't everywhere, and be within reach of the
    best match. One-word messages bring nothing."""
    words = len(set(tokens(str(question or ""))))
    if index is None or words < 2:
        return ""       # "hello", "thanks": nothing to look up (search_notes is there if needed)
    hits = index.search(question, limit=limit)
    top = hits[0]["score"] if hits else 0
    need = 0.6 if words <= 3 else 0.5
    hits = [h for h in hits if h["coverage"] >= need and h["telling"] and h["score"] >= 0.25 * top]
    if not hits:
        return ""
    lines = ["From the user's own notes (quote them when they answer the question; say which note):"]
    used = len(lines[0])
    for h in hits:
        where = h["title"] + (f" › {h['heading']}" if h["heading"] else "")
        line = f"- [{where}] {h['snippet']}"
        if used + len(line) > budget:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines) if len(lines) > 1 else ""


# ---------------------------------------------------------------------------
# Toby's own pages in your notes
# ---------------------------------------------------------------------------

MEMORY_HEADER = """# What Toby knows about you

Toby adds to this when you tell it something about yourself. It's Toby's
memory, not a copy of it: edit or delete lines here and Toby goes by what
you left.
"""


def toby_dir(root):
    return Path(root) / TOBY_DIR


def fact_id(text, category):
    return "m" + hashlib.sha1(f"{category}\n{text}".lower().encode()).hexdigest()[:10]


def read_memory(root):
    """The facts in <root>/Toby/Memory.md, as knowledge.py's fact records."""
    path = toby_dir(root) / MEMORY_FILE
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    facts, category = [], "general"
    for line in lines:
        heading = re.match(r"^##\s+(.*)", line)
        if heading:
            category = heading.group(1).strip().lower() or "general"
            continue
        item = re.match(r"^\s*[-*]\s+(?:\[[ x]\]\s+)?(.*\S)", line)
        if item:
            text = item.group(1).strip()
            facts.append({"id": fact_id(text, category), "text": text, "category": category})
    return facts


def write_memory(root, facts):
    """Rewrite Memory.md from fact records, grouped by category."""
    path = toby_dir(root) / MEMORY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    by_category = {}
    for f in facts:
        by_category.setdefault(f.get("category") or "general", []).append(f["text"])
    parts = [MEMORY_HEADER]
    for category in sorted(by_category):
        parts.append(f"\n## {category}\n")
        parts.extend(f"- {text}" for text in by_category[category])
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(parts).rstrip() + "\n")
    os.replace(tmp, path)


def safe_title(title):
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", " ", str(title or "")).strip(" .")
    return re.sub(r"\s+", " ", name)[:80] or "Note"


def add_note(root, title, text):
    """Save a note you asked for in <root>/Toby/Notes/<title>.md (added to
    the end if it's already there). Returns the path."""
    path = toby_dir(root) / "Notes" / f"{safe_title(title)}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    if path.exists():
        with path.open("a") as f:
            f.write(f"\n\n*{stamp}*\n\n{text.strip()}\n")
    else:
        path.write_text(f"# {safe_title(title)}\n\n*{stamp}, from Toby*\n\n{text.strip()}\n")
    return path


def log(root, line, when=None):
    """Add one line to today's log: <root>/Toby/Log/<date>.md."""
    when = when or datetime.now()
    path = toby_dir(root) / "Log" / f"{when:%Y-%m-%d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a") as f:
        if new:
            f.write(f"# Toby, {when:%A %d %B %Y}\n\n")
        f.write(f"- {when:%H:%M} {line.strip()}\n")
    return path
