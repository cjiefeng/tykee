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

ModelRole = Literal[
    "default", "escalated", "judge", "import_extract", "import_consolidate", "harvest"
]


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
    harvest: str = ""  # falls back to judge (both Haiku-tier) when unset

    def for_role(self, role: str) -> str:
        value = str(getattr(self, role))
        return value or (self.judge if role == "harvest" else value)


class RuntimeSettings(BaseModel):
    persona_system_prompt: str
    models: Models
    pricing: dict[str, Pricing] = Field(default_factory=dict)
    history_max_turns: int = 12
    llm_max_tokens: int = 400
    budget_daily_usd: float = 1.0
    budget_monthly_usd: float = 15.0
    decisions_session_hours: float = 6.0
    ambient_enabled: bool = True
    ambient_debounce_s: float = 30.0
    ambient_threshold: float = 0.75
    ambient_cooldown_min: float = 20.0
    ambient_max_per_day: int = 5
    ambient_window_messages: int = 20
    ambient_default_mute_min: float = 120.0
    ambient_negative_window_min: float = 15.0
    ambient_negative_phrases: list[str] = Field(default_factory=list)
    ambient_mute_phrases: list[str] = Field(default_factory=list)
    ambient_judge_prompt: str = ""
    summary_batch: int = 20
    embedding_precision: Literal["int8", "fp32"] = "int8"
    memory_auto_approve: bool = False
    memory_search_k: int = 6
    memory_pinned_max_chars: int = 6000
    telegram_answer_topic_id: int | None = None
    telegram_ignored_topic_ids: list[int] = Field(default_factory=list)
    telegram_off_topic_mention: Literal["ignore", "redirect"] = "ignore"
    harvest_enabled: bool = True
    harvest_interval_min: float = 30.0
    harvest_min_new_messages: int = 5
    harvest_max_age_hours: float = 6.0
    harvest_context_messages: int = 10
    budget_warn_ratio: float = 0.8

    def model_for(self, role: ModelRole) -> str:
        return self.models.for_role(role)

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
    values["ambient.judge_prompt"] = (SEED_DIR / "judge.md").read_text(encoding="utf-8").strip()
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
