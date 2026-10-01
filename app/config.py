"""Environment configuration. Secrets only come from env / .env (never settings table)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class AllowedUser:
    telegram_id: int
    slug: str
    is_admin: bool


def parse_allowlist(raw: str) -> list[AllowedUser]:
    """Parse ``111:jack,222:partner``. The first entry is the admin."""
    users: list[AllowedUser] = []
    for i, part in enumerate(p.strip() for p in raw.split(",") if p.strip()):
        tid, sep, slug = part.partition(":")
        if not sep or not slug.strip():
            raise ValueError(f"ALLOWED_TELEGRAM_IDS entry {part!r} must look like <id>:<slug>")
        users.append(AllowedUser(int(tid), slug.strip().lower(), is_admin=(i == 0)))
    if len({u.telegram_id for u in users}) != len(users) or len({u.slug for u in users}) != len(
        users
    ):
        raise ValueError("ALLOWED_TELEGRAM_IDS contains duplicate ids or slugs")
    if not users:
        raise ValueError("ALLOWED_TELEGRAM_IDS must list at least one user")
    return users


class Env(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str
    anthropic_api_key: str = ""  # empty -> fallback mode (§7.0)
    allowed_telegram_ids: str
    group_chat_id: int | None = None
    data_dir: Path = Path("/data")
    log_level: str = "INFO"
    tz: str = "UTC"

    @field_validator("allowed_telegram_ids")
    @classmethod
    def _validate_allowlist(cls, v: str) -> str:
        parse_allowlist(v)
        return v

    @property
    def allowlist(self) -> list[AllowedUser]:
        return parse_allowlist(self.allowed_telegram_ids)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bot.db"
