"""Local, inspectable conversation memory for Salus."""

from __future__ import annotations

import json
import random
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class HistoryStore:
    def __init__(self, data_dir: Path, library_path: Path):
        self.data_dir = data_dir
        self.db_path = data_dir / "salus.db"
        self.doc_path = data_dir / "history.md"
        self.library = json.loads(library_path.read_text(encoding="utf-8"))["questions"]
        demo_path = Path(__file__).with_name("demo_memories.md")
        self.demo_memories = demo_path.read_text(encoding="utf-8") if demo_path.exists() else ""
        data_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS check_ins (
                id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL, question_id TEXT,
                question TEXT NOT NULL, answer TEXT NOT NULL, response TEXT NOT NULL,
                response_source TEXT NOT NULL, safety_pause INTEGER NOT NULL, memory_note TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS memory (
                id INTEGER PRIMARY KEY CHECK (id = 1), summary TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")

    def settings(self) -> dict[str, object]:
        defaults = {"spoken_guidance": True, "spoken_check_ins": True, "robot_colour": "#1a6964"}
        with self._connect() as db:
            for key, value in db.execute("SELECT key, value FROM settings"):
                defaults[key] = json.loads(value)
        return defaults

    def save_settings(self, values: dict[str, object]) -> dict[str, object]:
        allowed = {"spoken_guidance", "spoken_check_ins", "robot_colour"}
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            for key, value in values.items():
                if key in allowed:
                    db.execute("INSERT INTO settings VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (key, json.dumps(value), now))
        return self.settings()

    def _connect(self):
        return sqlite3.connect(self.db_path, timeout=5)

    def context(self) -> str:
        with self._connect() as db:
            row = db.execute("SELECT summary FROM memory WHERE id=1").fetchone()
            recent = db.execute(
                "SELECT question, answer, response FROM check_ins ORDER BY id DESC LIMIT 5"
            ).fetchall()
        summary = row[0] if row else "No saved preferences yet."
        turns = "\n".join(
            f"- Salus asked: {question}\n  Person said: {answer}\n  Salus replied: {response}"
            for question, answer, response in reversed(recent)
        )
        return (
            f"Fictional demo memories:\n{self.demo_memories or 'None.'}\n\n"
            f"Saved notes:\n{summary}\n\nRecent conversation:\n{turns or 'None yet.'}"
        )[:5000]

    def next_question(self) -> dict[str, str]:
        with self._connect() as db:
            used = {row[0] for row in db.execute(
                "SELECT question_id FROM check_ins WHERE question_id IS NOT NULL ORDER BY id DESC LIMIT 4"
            )}
        choices = [item for item in self.library if item["id"] not in used] or self.library
        return random.choice(choices)

    def record(self, *, question_id: str | None, question: str, answer: str, response: str,
               response_source: str, safety_pause: bool, memory_note: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        clean_note = " ".join(memory_note.split())[:240]
        with self._connect() as db:
            db.execute(
                """INSERT INTO check_ins
                (recorded_at, question_id, question, answer, response, response_source, safety_pause, memory_note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (now, question_id, question, answer, response, response_source, int(safety_pause), clean_note),
            )
            if clean_note:
                old = db.execute("SELECT summary FROM memory WHERE id=1").fetchone()
                notes = [line.removeprefix("- ") for line in (old[0].splitlines() if old else []) if line.strip()]
                if clean_note.casefold() not in {note.casefold() for note in notes}:
                    notes.append(clean_note)
                summary = "\n".join(f"- {note}" for note in notes[-12:])
                db.execute(
                    "INSERT INTO memory VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET summary=excluded.summary, updated_at=excluded.updated_at",
                    (summary, now),
                )
        self._write_doc()

    def _write_doc(self) -> None:
        with self._connect() as db:
            summary = db.execute("SELECT summary FROM memory WHERE id=1").fetchone()
            turns = db.execute(
                "SELECT recorded_at, question, answer FROM check_ins ORDER BY id DESC LIMIT 20"
            ).fetchall()
        lines = ["# Salus local history", "", "This file stays on this computer.", "", "## Saved notes", "", summary[0] if summary else "No saved notes yet.", "", "## Recent answers", ""]
        for at, question, answer in turns:
            lines.extend([f"### {at}", "", f"**Salus:** {question}", "", f"**Answer:** {answer}", ""])
        self.doc_path.write_text("\n".join(lines), encoding="utf-8")
