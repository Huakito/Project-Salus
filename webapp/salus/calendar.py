"""Small local calendar used to demonstrate future Google Calendar context."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


class DemoCalendar:
    def __init__(self, data_dir: Path):
        self.db_path = data_dir / "salus.db"
        data_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS calendar_events (
                id INTEGER PRIMARY KEY, title TEXT NOT NULL, starts_at TEXT NOT NULL,
                kind TEXT NOT NULL, source TEXT NOT NULL
            )""")
            if not db.execute("SELECT 1 FROM calendar_events WHERE source='demo' LIMIT 1").fetchone():
                now = datetime.now(timezone.utc)
                demo = [
                    ("Work on the Salus prototype", now + timedelta(days=1, hours=2), "project"),
                    ("Coffee with Alex", now + timedelta(days=3, hours=1), "social"),
                    ("School project deadline", now + timedelta(days=6), "deadline"),
                ]
                db.executemany(
                    "INSERT INTO calendar_events (title, starts_at, kind, source) VALUES (?, ?, ?, 'demo')",
                    [(title, start.isoformat(), kind) for title, start, kind in demo],
                )

    def _connect(self):
        return sqlite3.connect(self.db_path, timeout=5)

    def upcoming(self, limit: int = 10) -> list[dict[str, str]]:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            rows = db.execute(
                """SELECT id, title, starts_at, kind, source FROM calendar_events
                   WHERE starts_at >= ? ORDER BY starts_at LIMIT ?""",
                (now, limit),
            ).fetchall()
        return [
            {"id": str(row[0]), "title": row[1], "starts_at": row[2], "kind": row[3], "source": row[4]}
            for row in rows
        ]

    def question(self) -> dict[str, str] | None:
        events = self.upcoming(1)
        if not events:
            return None
        event = events[0]
        start = datetime.fromisoformat(event["starts_at"])
        days = max(0, (start.date() - datetime.now(timezone.utc).date()).days)
        when = "tomorrow" if days == 1 else ("today" if days == 0 else f"in {days} days")
        prompts = {
            "social": f"You have {event['title']} {when}. Are you looking forward to it?",
            "deadline": f"Your {event['title']} is {when}. Is there anything you want to get sorted beforehand?",
            "project": f"You have {event['title']} planned for {when}. What part do you want to work on?",
        }
        return {
            "id": f"calendar-{event['id']}",
            "level": "light",
            "topic": "calendar",
            "text": prompts.get(event["kind"], f"You have {event['title']} {when}. How does that sound?"),
            "event_id": event["id"],
        }
