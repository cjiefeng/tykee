"""Runtime settings stored in the ``settings`` table (hot-reloaded: read on every request).

Seed values live in ``app/seed/`` and are inserted only for keys that don't exist, so dashboard
edits are never overwritten on restart.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

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


DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _hhmm(value: str) -> str:
    hh, sep, mm = value.strip().partition(":")
    if not (sep and hh.isdigit() and mm.isdigit() and int(hh) < 24 and int(mm) < 60):
        raise ValueError("time must be HH:MM (24h)")
    return f"{int(hh):02d}:{int(mm):02d}"


class Nudge(BaseModel):
    """One scheduled nudge (§10.3): at ``time`` on ``days``, pick from ``category`` and post it
    to ``target`` ('group' or a user slug for a DM)."""

    id: str = Field(min_length=1, max_length=32, pattern=r"^[a-z0-9_-]+$")
    time: str
    days: list[str] = Field(min_length=1)
    category: str = Field(min_length=1)
    target: str = "group"
    enabled: bool = True

    @field_validator("time")
    @classmethod
    def _time(cls, value: str) -> str:
        return _hhmm(value)

    @field_validator("days")
    @classmethod
    def _days(cls, value: list[str]) -> list[str]:
        days = {d.strip().lower()[:3] for d in value}
        if not days <= set(DAYS):
            raise ValueError(f"days must be among {', '.join(DAYS)}")
        return [d for d in DAYS if d in days]


class WebLocation(BaseModel):
    """Approximate location that localises web search results (§7.5)."""

    type: Literal["approximate"] = "approximate"
    city: str | None = None
    region: str | None = None
    country: str | None = Field(None, pattern=r"^[A-Z]{2}$")  # ISO 3166-1 alpha-2
    timezone: str | None = None


class WebToolVersions(BaseModel):
    """Server tool type strings (§7.5), from current Anthropic docs; never hardcoded in code."""

    web_search: str = Field(min_length=1, pattern=r"^web_search_\d{8}$")
    web_fetch: str = Field(min_length=1, pattern=r"^web_fetch_\d{8}$")


def _domains(value: list[str]) -> list[str]:
    out = [d.strip().lower() for d in value if d.strip()]
    if any("://" in d or " " in d for d in out):
        raise ValueError("domains are bare hosts like example.com (no scheme, no spaces)")
    return out


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
    import_max_upload_mb: int = 200
    import_poll_min: float = 5.0
    nudges_enabled: bool = False
    nudges_grace_min: float = 30.0
    nudges_items: list[Nudge] = Field(default_factory=list)
    backup_enabled: bool = True
    backup_time: str = "03:00"
    backup_keep: int = Field(14, ge=1)
    web_enabled: bool = False
    web_search_max_uses: int = Field(3, ge=1, le=10)
    web_fetch_max_uses: int = Field(2, ge=0, le=10)  # 0 → no web_fetch tool
    web_fetch_max_content_tokens: int = Field(4000, ge=500)
    web_daily_search_cap: int = Field(50, ge=0)
    web_user_location: WebLocation | None = None
    web_allowed_domains: list[str] = Field(default_factory=list)
    web_blocked_domains: list[str] = Field(default_factory=list)
    web_tool_versions: WebToolVersions | None = None  # None → web tools stay off
    pricing_web_search: float = Field(0.0, ge=0)  # USD per search (`pricing.web_search`)

    @field_validator("web_allowed_domains", "web_blocked_domains")
    @classmethod
    def _web_domains(cls, value: list[str]) -> list[str]:
        return _domains(value)

    @model_validator(mode="after")
    def _one_domain_list(self) -> RuntimeSettings:
        if self.web_allowed_domains and self.web_blocked_domains:
            raise ValueError("set web.allowed_domains or web.blocked_domains, not both")
        return self

    @field_validator("backup_time")
    @classmethod
    def _backup_time(cls, value: str) -> str:
        return _hhmm(value)

    @field_validator("nudges_items")
    @classmethod
    def _unique_nudge_ids(cls, value: list[Nudge]) -> list[Nudge]:
        ids = [n.id for n in value]
        if len(ids) != len(set(ids)):
            raise ValueError("nudge ids must be unique")
        return value

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
            elif key == "pricing.web_search":  # a per-search price, not a model's token prices
                flat["pricing_web_search"] = value
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
