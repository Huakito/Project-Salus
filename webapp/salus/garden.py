"""Persistent, forgiving virtual garden state."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

CATALOG = {
    "daisy": {"name": "Daisy", "sprite": 31, "care": "water", "care_label": "Water lightly", "about": "Daisies like bright light and soil that can drain. Water when the top soil begins to dry."},
    "sunflower": {"name": "Sunflower", "sprite": 45, "care": "water", "care_label": "Give it a drink", "about": "Sunflowers turn toward strong light and usually need regular water while growing."},
    "lavender": {"name": "Lavender", "sprite": 54, "care": "sun", "care_label": "Find some sun", "about": "Lavender prefers plenty of sun and does better when its soil is not kept constantly wet."},
    "tulip": {"name": "Tulip", "sprite": 42, "care": "check", "care_label": "Check the soil", "about": "Tulips grow from bulbs. They enjoy bright conditions and modest watering rather than soaked soil."},
    "cattail": {"name": "Cattail", "sprite": 17, "care": "water", "care_label": "Top up the water", "about": "Cattails naturally grow at wetland edges, so they are much happier with consistently damp soil."},
    "vine": {"name": "Climbing vine", "sprite": 12, "care": "turn", "care_label": "Turn the pot", "about": "Climbing plants follow light. Turning the pot occasionally can encourage more even growth."},
    "mushroom": {"name": "Woodland mushroom", "sprite": 72, "care": "mist", "care_label": "Mist the patch", "about": "Mushrooms are fungi rather than plants. Their fruiting bodies often appear in cool, damp conditions."},
    "wildflower": {"name": "Wildflower", "sprite": 48, "care": "check", "care_label": "Look for new growth", "about": "Wildflowers vary widely, but many support insects and cope well without intensive care once established."},
}


class GardenStore:
    def __init__(self, data_dir: Path):
        self.db_path = data_dir / "salus.db"
        data_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS garden_meta
                (key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS garden_plants (
                id INTEGER PRIMARY KEY, species TEXT NOT NULL, nickname TEXT NOT NULL,
                growth INTEGER NOT NULL DEFAULT 0, planted_day TEXT NOT NULL,
                last_cared_day TEXT, care_count INTEGER NOT NULL DEFAULT 0
            )""")
            if not db.execute("SELECT 1 FROM garden_meta WHERE key='day_offset'").fetchone():
                db.execute("INSERT INTO garden_meta VALUES ('day_offset', '0')")
            if not db.execute("SELECT 1 FROM garden_plants").fetchone():
                today = self._day(db)
                db.execute("INSERT INTO garden_plants(species,nickname,growth,planted_day) VALUES ('daisy','Dot',1,?)", (today,))

    def _connect(self):
        return sqlite3.connect(self.db_path, timeout=5)

    def _day(self, db) -> str:
        row = db.execute("SELECT value FROM garden_meta WHERE key='day_offset'").fetchone()
        return (date.today() + timedelta(days=int(row[0]) if row else 0)).isoformat()

    def state(self) -> dict:
        with self._connect() as db:
            today = self._day(db)
            rows = db.execute("SELECT id,species,nickname,growth,planted_day,last_cared_day,care_count FROM garden_plants ORDER BY id").fetchall()
        plants = []
        for plant_id, species, nickname, growth, planted, last_cared, count in rows:
            plants.append({"id": plant_id, "species": species, "nickname": nickname,
                           "growth": growth, "planted_day": planted, "last_cared_day": last_cared,
                           "care_count": count, "needs_care": last_cared != today, **CATALOG[species]})
        return {"today": today, "catalog": [{"species": key, **value} for key, value in CATALOG.items()], "plants": plants}

    def add(self, species: str, nickname: str) -> dict:
        if species not in CATALOG:
            raise ValueError("That plant is not in the seed shelf.")
        clean = " ".join(nickname.split())[:30] or CATALOG[species]["name"]
        with self._connect() as db:
            cursor = db.execute("INSERT INTO garden_plants(species,nickname,planted_day) VALUES (?,?,?)", (species, clean, self._day(db)))
            plant_id = cursor.lastrowid
        return next(p for p in self.state()["plants"] if p["id"] == plant_id)

    def care(self, plant_id: int) -> tuple[dict, bool]:
        with self._connect() as db:
            today = self._day(db)
            row = db.execute("SELECT last_cared_day FROM garden_plants WHERE id=?", (plant_id,)).fetchone()
            if not row:
                raise ValueError("That plant is not in the garden.")
            changed = row[0] != today
            if changed:
                db.execute("UPDATE garden_plants SET last_cared_day=?, growth=MIN(5,growth+1), care_count=care_count+1 WHERE id=?", (today, plant_id))
        return next(p for p in self.state()["plants"] if p["id"] == plant_id), changed

    def admin(self, action: str, days: int = 1) -> dict:
        with self._connect() as db:
            if action == "advance_day":
                old = int(db.execute("SELECT value FROM garden_meta WHERE key='day_offset'").fetchone()[0])
                db.execute("UPDATE garden_meta SET value=? WHERE key='day_offset'", (str(old + max(1, min(days, 30))),))
            elif action == "grow_all":
                db.execute("UPDATE garden_plants SET growth=MIN(5,growth+1)")
            elif action == "make_due":
                db.execute("UPDATE garden_plants SET last_cared_day=NULL")
            else:
                raise ValueError("Unknown admin action.")
        return self.state()
