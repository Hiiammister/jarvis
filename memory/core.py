"""
memory/core.py — Hermes-style persistent memory for Jarvis.

Two flat markdown files, mirroring Hermes Agent's architecture:
  ~/.jarvis/MEMORY.md  — agent's personal notes (env facts, learned conventions)
  ~/.jarvis/USER.md    — user profile (preferences, style, identity)

Both are injected into the system prompt as a frozen snapshot at session start.
The agent edits them via tool calls (add / replace / remove).

SQLite + FTS5 for full session history with cross-session search.
"""

import os
import re
import sqlite3
import datetime
import tempfile
from pathlib import Path


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

JARVIS_HOME = Path.home() / ".jarvis"

# Use Hermes's memory files if Hermes is installed — shared memory across both agents
_HERMES_MEM = Path.home() / ".hermes" / "memories"
MEMORY_FILE = _HERMES_MEM / "MEMORY.md" if _HERMES_MEM.exists() else JARVIS_HOME / "MEMORY.md"
USER_FILE   = _HERMES_MEM / "USER.md"   if _HERMES_MEM.exists() else JARVIS_HOME / "USER.md"

DB_FILE = JARVIS_HOME / "sessions.db"

MEMORY_LIMIT = 2200   # chars, matching Hermes
USER_LIMIT = 1375     # chars, matching Hermes
ENTRY_SEP = "§"       # Hermes section-sign separator


# ── Bootstrap ─────────────────────────────────────────────────────────────────

def _ensure_home():
    JARVIS_HOME.mkdir(parents=True, exist_ok=True)
    # Ensure memory files exist (Hermes creates them on first use; we do the same)
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not MEMORY_FILE.exists():
        MEMORY_FILE.write_text("", encoding="utf-8")
    if not USER_FILE.exists():
        USER_FILE.write_text("", encoding="utf-8")


def _get_db() -> sqlite3.Connection:
    _ensure_home()
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            started   TEXT NOT NULL,
            ended     TEXT,
            summary   TEXT
        );
        CREATE TABLE IF NOT EXISTS messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES sessions(id),
            ts         TEXT NOT NULL,
            role       TEXT NOT NULL,
            content    TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
            USING fts5(content, content='messages', content_rowid='id');
        CREATE TRIGGER IF NOT EXISTS messages_ai
            AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
            END;
        CREATE TRIGGER IF NOT EXISTS messages_ad
            AFTER DELETE ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, content)
                    VALUES ('delete', old.id, old.content);
            END;
    """)
    conn.commit()
    return conn


# ── Markdown file helpers ──────────────────────────────────────────────────────

def _read_entries(path: Path) -> list[str]:
    """Read §-separated entries from a memory file."""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    return [e.strip() for e in text.split(ENTRY_SEP) if e.strip()]


def _write_entries(path: Path, entries: list[str]) -> None:
    """Atomically write entries so a crash mid-write can't corrupt the file."""
    text = f"\n{ENTRY_SEP}\n".join(entries)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _char_count(entries: list[str]) -> int:
    if not entries:
        return 0
    return len(f"\n{ENTRY_SEP}\n".join(entries))


def _path_for(target: str) -> Path:
    if target == "user":
        return USER_FILE
    return MEMORY_FILE


def _limit_for(target: str) -> int:
    return USER_LIMIT if target == "user" else MEMORY_LIMIT


# ── Public memory API (called by tool handlers) ───────────────────────────────

def memory_add(target: str, content: str) -> dict:
    """Add a new entry to MEMORY.md or USER.md."""
    _ensure_home()
    path = _path_for(target)
    limit = _limit_for(target)
    entries = _read_entries(path)
    current = _char_count(entries)
    needed = len(content) + (len(ENTRY_SEP) + 2 if entries else 0)
    if current + needed > limit:
        return {
            "success": False,
            "error": (
                f"Memory at {current}/{limit} chars. Adding this entry "
                f"({len(content)} chars) would exceed the limit. "
                "Use replace to consolidate or remove to make room first."
            ),
        }
    entries.append(content)
    _write_entries(path, entries)
    new_count = _char_count(entries)
    return {"success": True, "chars": new_count, "limit": limit, "entries": len(entries)}


def memory_replace(target: str, old_text: str, new_content: str) -> dict:
    """Replace an entry identified by a unique substring."""
    _ensure_home()
    path = _path_for(target)
    limit = _limit_for(target)
    entries = _read_entries(path)
    matches = [i for i, e in enumerate(entries) if old_text in e]
    if len(matches) == 0:
        return {"success": False, "error": f"No entry contains '{old_text}'."}
    if len(matches) > 1:
        return {"success": False, "error": f"'{old_text}' matches {len(matches)} entries — be more specific."}
    new_entries = entries[:]
    new_entries[matches[0]] = new_content
    new_count = _char_count(new_entries)
    if new_count > limit:
        return {"success": False, "error": f"Replacement would exceed limit ({new_count}/{limit} chars). Shorten the new content."}
    _write_entries(path, new_entries)
    return {"success": True, "chars": new_count, "limit": limit}


def memory_remove(target: str, old_text: str) -> dict:
    """Remove an entry identified by a unique substring."""
    _ensure_home()
    path = _path_for(target)
    entries = _read_entries(path)
    matches = [i for i, e in enumerate(entries) if old_text in e]
    if len(matches) == 0:
        return {"success": False, "error": f"No entry contains '{old_text}'."}
    if len(matches) > 1:
        return {"success": False, "error": f"'{old_text}' matches {len(matches)} entries — be more specific."}
    entries.pop(matches[0])
    _write_entries(path, entries)
    limit = _limit_for(target)
    return {"success": True, "chars": _char_count(entries), "limit": limit, "entries": len(entries)}


def memory_read(target: str) -> dict:
    """Read current memory entries (for tool response display)."""
    _ensure_home()
    path = _path_for(target)
    entries = _read_entries(path)
    limit = _limit_for(target)
    chars = _char_count(entries)
    pct = int(chars / limit * 100)
    return {"entries": entries, "chars": chars, "limit": limit, "pct": pct}


# ── System prompt injection ────────────────────────────────────────────────────

def build_memory_block() -> str:
    """Build the frozen memory block injected into the system prompt."""
    _ensure_home()
    blocks = []
    for target, path, limit, label in [
        ("memory", MEMORY_FILE, MEMORY_LIMIT, "MEMORY (your personal notes)"),
        ("user",   USER_FILE,   USER_LIMIT,   "USER PROFILE"),
    ]:
        entries = _read_entries(path)
        chars = _char_count(entries)
        pct = int(chars / limit * 100) if entries else 0
        header = f"══════════════════════════════════════════════\n{label} [{pct}% — {chars}/{limit} chars]\n══════════════════════════════════════════════"
        if entries:
            body = f"\n{ENTRY_SEP}\n".join(entries)
        else:
            body = "(empty)"
        blocks.append(f"{header}\n{body}")
    return "\n\n".join(blocks)


# ── Session history (SQLite) ───────────────────────────────────────────────────

class Session:
    def __init__(self):
        self.conn = _get_db()
        now = _utc_now_iso()
        cur = self.conn.execute("INSERT INTO sessions (started) VALUES (?)", (now,))
        self.conn.commit()
        self.session_id = cur.lastrowid

    def add_message(self, role: str, content: str) -> None:
        now = _utc_now_iso()
        self.conn.execute(
            "INSERT INTO messages (session_id, ts, role, content) VALUES (?, ?, ?, ?)",
            (self.session_id, now, role, content),
        )
        self.conn.commit()

    def close(self, summary: str = "") -> None:
        now = _utc_now_iso()
        self.conn.execute(
            "UPDATE sessions SET ended=?, summary=? WHERE id=?",
            (now, summary, self.session_id),
        )
        self.conn.commit()
        self.conn.close()

    def get_history(self, k: int = 50) -> list[dict]:
        """Return the last k messages for this session."""
        rows = self.conn.execute(
            "SELECT role, content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (self.session_id, k),
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def _fts_query(raw: str) -> str:
    """Turn arbitrary user text into a safe FTS5 MATCH expression.

    Bare punctuation (-, ", *, :, etc.) is valid FTS5 syntax and makes MATCH
    raise OperationalError, so treat each word as a quoted term instead.
    """
    words = re.findall(r"\w+", raw)
    return " ".join(f'"{w}"' for w in words)


def search_history(query: str, limit: int = 5) -> list[dict]:
    """FTS5 full-text search across all sessions."""
    match = _fts_query(query)
    if not match:
        return []
    conn = _get_db()
    try:
        rows = conn.execute(
            """
            SELECT m.role, m.content, m.ts, s.started
            FROM messages_fts f
            JOIN messages m ON m.id = f.rowid
            JOIN sessions s ON s.id = m.session_id
            WHERE messages_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (match, limit),
        ).fetchall()
    finally:
        conn.close()
    return [{"role": r["role"], "content": r["content"], "ts": r["ts"], "session_started": r["started"]} for r in rows]
