"""Environment configuration. Secrets only come from env / .env (never settings table)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


def dashboard_configured(password_hash: str, session_secret: str) -> bool:
    """No default credentials (§11): both secrets, and a long enough session secret."""
    return bool(password_hash) and len(session_secret) >= 32


class Env(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    telegram_bot_token: str
    anthropic_api_key: str = ""  # empty -> fallback mode (§7.0)
    allowed_telegram_ids: str
    group_chat_id: int | None = None  # see design §10: seeds the one allowed group
    group_topic_id: int | None = None  # §10.4: seeds telegram.answer_topic_id on first run
    dashboard_password_hash: str = ""  # argon2id; empty → dashboard login disabled
    session_secret: str = ""
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8080
    data_dir: Path = Path("/data")
    embed_baked_dir: Path = Path("/app/models")  # model files baked into the image at build
    log_level: str = "INFO"
    tz: str = "UTC"
    # §10.7 account reader: my.telegram.org app + the key that encrypts the saved login. The key
    # stays out of /data, so backups never hold a usable session.
    tg_api_id: int | None = None
    tg_api_hash: str = ""
    reader_session_key: str = ""

    @field_validator("allowed_telegram_ids")
    @classmethod
    def _validate_allowlist(cls, v: str) -> str:
        parse_allowlist(v)
        return v

    @field_validator("tz")
    @classmethod
    def _validate_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as e:  # a KeyError, so pydantic wouldn't wrap it
            raise ValueError(f"unknown timezone {v!r}") from e
        return v

    @property
    def allowlist(self) -> list[AllowedUser]:
        return parse_allowlist(self.allowed_telegram_ids)

    @property
    def dashboard_enabled(self) -> bool:
        return dashboard_configured(self.dashboard_password_hash, self.session_secret)

    @property
    def reader_configured(self) -> bool:
        return bool(self.tg_api_id and self.tg_api_hash and self.reader_session_key)

    @property
    def reader_session_path(self) -> Path:
        return self.data_dir / "reader.session.enc"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bot.db"
