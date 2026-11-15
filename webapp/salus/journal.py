"""Local journal entries and context for companion conversations."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


class JournalStore:
    def __init__(self, data_dir: Path):
        self.db_path = data_dir / "salus.db"
        data_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS journal_entries (
                id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL, body TEXT NOT NULL,
                source TEXT NOT NULL
            )""")
            if not db.execute("SELECT 1 FROM journal_entries LIMIT 1").fetchone():
                now = datetime.now(timezone.utc)
                demo = [
                    (now - timedelta(days=2), "Spent most of the afternoon working on Salus. The voice finally sounded right, which felt like real progress. I was tired by the evening but pleased with it."),
                    (now - timedelta(days=1), "Had a slower morning and went out for coffee with Alex. It was good to get away from the laptop. I want to remember to make time for that when project work gets busy."),
                    (now, "The project deadline is getting closer. I still need to polish the movement demo, but breaking it into small jobs is making it feel manageable."),
                ]
                db.executemany(
                    "INSERT INTO journal_entries (recorded_at, body, source) VALUES (?, ?, 'demo')",
                    [(at.isoformat(), body) for at, body in demo],
                )

    def _connect(self):
        return sqlite3.connect(self.db_path, timeout=5)

    def entries(self, limit: int = 30) -> list[dict[str, str]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, recorded_at, body, source FROM journal_entries ORDER BY recorded_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{"id": str(r[0]), "recorded_at": r[1], "body": r[2], "source": r[3]} for r in rows]

    def add(self, body: str, source: str = "user") -> dict[str, str]:
        now = datetime.now(timezone.utc).isoformat()
        clean = " ".join(body.split())
        with self._connect() as db:
            cursor = db.execute(
                "INSERT INTO journal_entries (recorded_at, body, source) VALUES (?, ?, ?)",
                (now, clean, source),
            )
        return {"id": str(cursor.lastrowid), "recorded_at": now, "body": clean, "source": source}

    def context(self) -> str:
        return "\n".join(
            f"- {entry['recorded_at'][:10]}: {entry['body']}" for entry in reversed(self.entries(10))
        )[:5000]
