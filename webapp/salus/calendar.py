"""Local demo calendar with an optional real Google Calendar connection."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from urllib.parse import urlencode

import httpx
from datetime import datetime, timedelta, timezone
from pathlib import Path


class DemoCalendar:
    def __init__(self, data_dir: Path):
        self.db_path = data_dir / "salus.db"
        self.token_path = data_dir / "google-calendar-token.json"
        self.client_id = os.environ.get("GOOGLE_CALENDAR_CLIENT_ID", "")
        self.client_secret = os.environ.get("GOOGLE_CALENDAR_CLIENT_SECRET", "")
        self.oauth_state: str | None = None
        self.redirect_uri = "http://127.0.0.1:8000/api/calendar/google/callback"
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

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def connected(self) -> bool:
        return self.configured and self.token_path.exists()

    def status(self) -> dict[str, object]:
        return {"configured": self.configured, "connected": self.connected, "source": "google" if self.connected else "demo"}

    def auth_url(self) -> str:
        if not self.configured:
            raise ValueError("Google Calendar OAuth credentials have not been added yet.")
        self.oauth_state = secrets.token_urlsafe(24)
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": "https://www.googleapis.com/auth/calendar.events",
            "access_type": "offline",
            "prompt": "consent",
            "state": self.oauth_state,
        })

    def finish_auth(self, code: str, state: str) -> None:
        if not self.oauth_state or not secrets.compare_digest(state, self.oauth_state):
            raise ValueError("Google returned an invalid sign-in state. Please try connecting again.")
        response = httpx.post("https://oauth2.googleapis.com/token", data={
            "code": code, "client_id": self.client_id, "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri, "grant_type": "authorization_code",
        }, timeout=10)
        response.raise_for_status()
        self.token_path.write_text(json.dumps(response.json()), encoding="utf-8")
        self.oauth_state = None

    def disconnect(self) -> None:
        self.token_path.unlink(missing_ok=True)

    def _access_token(self) -> str:
        token = json.loads(self.token_path.read_text(encoding="utf-8"))
        if token.get("refresh_token"):
            response = httpx.post("https://oauth2.googleapis.com/token", data={
                "client_id": self.client_id, "client_secret": self.client_secret,
                "refresh_token": token["refresh_token"], "grant_type": "refresh_token",
            }, timeout=10)
            response.raise_for_status()
            refreshed = response.json()
            token.update(refreshed)
            self.token_path.write_text(json.dumps(token), encoding="utf-8")
        return token["access_token"]

    def _google_upcoming(self, limit: int) -> list[dict[str, str]]:
        response = httpx.get(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            headers={"Authorization": f"Bearer {self._access_token()}"},
            params={"timeMin": datetime.now(timezone.utc).isoformat(), "maxResults": limit, "singleEvents": "true", "orderBy": "startTime"},
            timeout=10,
        )
        response.raise_for_status()
        events = []
        for item in response.json().get("items", []):
            start = item.get("start", {}).get("dateTime") or item.get("start", {}).get("date")
            if start:
                events.append({"id": item["id"], "title": item.get("summary", "Untitled event"), "starts_at": start, "kind": "google", "source": "google"})
        return events

    def upcoming(self, limit: int = 10) -> list[dict[str, str]]:
        if self.connected:
            try:
                return self._google_upcoming(limit)
            except Exception:
                # Keep the app usable if Google is temporarily unreachable.
                pass
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

    def add(self, title: str, starts_at: datetime, source: str = "conversation") -> dict[str, str]:
        if self.connected:
            end = starts_at + timedelta(hours=1)
            response = httpx.post(
                "https://www.googleapis.com/calendar/v3/calendars/primary/events",
                headers={"Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json"},
                json={"summary": title, "start": {"dateTime": starts_at.isoformat()}, "end": {"dateTime": end.isoformat()}},
                timeout=10,
            )
            response.raise_for_status()
            item = response.json()
            return {"id": item["id"], "title": title, "starts_at": starts_at.isoformat(), "kind": "google", "source": "google"}
        with self._connect() as db:
            cursor = db.execute(
                "INSERT INTO calendar_events (title, starts_at, kind, source) VALUES (?, ?, 'personal', ?)",
                (title, starts_at.astimezone(timezone.utc).isoformat(), source),
            )
        return {"id": str(cursor.lastrowid), "title": title, "starts_at": starts_at.isoformat(), "kind": "personal", "source": source}

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
