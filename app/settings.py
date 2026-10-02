"""Runtime settings stored in the ``settings`` table (hot-reloaded: read on every request).

Seed values live in ``app/seed/`` and are inserted only for keys that don't exist, so dashboard
edits are never overwritten on restart.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.db.database import Database

SEED_DIR = Path(__file__).parent / "seed"

ModelRole = Literal["default", "escalated", "judge", "import_extract", "import_consolidate"]


class Pricing(BaseModel):
    """USD per million tokens."""

    input: float
    output: float
    cache_write: float
    cache_read: float


class Models(BaseModel):
    default: str
    escalated: str
    judge: str
    import_extract: str
    import_consolidate: str


class RuntimeSettings(BaseModel):
    persona_system_prompt: str
    models: Models
    pricing: dict[str, Pricing] = Field(default_factory=dict)
    history_max_turns: int = 12
    llm_max_tokens: int = 400
    budget_daily_usd: float = 1.0
    budget_monthly_usd: float = 15.0
    decisions_session_hours: float = 6.0

    def model_for(self, role: ModelRole) -> str:
        return str(getattr(self.models, role))

    @classmethod
    def from_rows(cls, rows: dict[str, Any]) -> RuntimeSettings:
        models: dict[str, Any] = {}
        pricing: dict[str, Any] = {}
        flat: dict[str, Any] = {}
        for key, value in rows.items():
            group, _, name = key.partition(".")
            if group == "models":
                models[name] = value
            elif group == "pricing":
                pricing[name] = value
            else:
                flat[key.replace(".", "_")] = value
        return cls.model_validate({**flat, "models": models, "pricing": pricing})


def seed_values() -> dict[str, Any]:
    raw: dict[str, Any] = json.loads((SEED_DIR / "settings.json").read_text(encoding="utf-8"))
    values = {k: v for k, v in raw.items() if not k.startswith("_")}
    values["persona.system_prompt"] = (SEED_DIR / "persona.md").read_text(encoding="utf-8").strip()
    return values


def seed_settings(conn: sqlite3.Connection) -> int:
    """Insert missing keys. Returns the number of keys inserted."""
    inserted = 0
    for key, value in seed_values().items():
        cur = conn.execute(
            "INSERT OR IGNORE INTO settings(key, value_json) VALUES (?, ?)",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        inserted += cur.rowcount
    return inserted


def _read_all(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        row["key"]: json.loads(row["value_json"])
        for row in conn.execute("SELECT key, value_json FROM settings")
    }


def set_value(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET "
        "value_json = excluded.value_json, updated_at = excluded.updated_at",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def get_value(conn: sqlite3.Connection, key: str) -> Any:
    row = conn.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
    return None if row is None else json.loads(row["value_json"])


class SettingsStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def load(self) -> RuntimeSettings:
        rows = await self._db.read(_read_all)
        return RuntimeSettings.from_rows(rows)
