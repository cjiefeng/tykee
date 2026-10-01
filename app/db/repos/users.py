"""users table: seeded from the env allowlist (§10)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.config import AllowedUser


@dataclass(frozen=True)
class UserRecord:
    id: int
    telegram_id: int
    slug: str
    display_name: str
    timezone: str
    is_admin: bool


def upsert_allowlist(conn: sqlite3.Connection, allowlist: list[AllowedUser], tz: str) -> None:
    """Insert allowlisted users, keep existing display names/timezones, and disable anyone who
    was removed from the allowlist."""
    for u in allowlist:
        conn.execute(
            "INSERT INTO users(telegram_id, slug, display_name, timezone, enabled) "
            "VALUES (?, ?, ?, ?, 1) "
            "ON CONFLICT(telegram_id) DO UPDATE SET slug=excluded.slug, enabled=1",
            (u.telegram_id, u.slug, u.slug.capitalize(), tz),
        )
    ids = [u.telegram_id for u in allowlist]
    conn.execute(
        f"UPDATE users SET enabled = 0 WHERE telegram_id NOT IN ({','.join('?' * len(ids))})",
        ids,
    )


def load_enabled(conn: sqlite3.Connection, allowlist: list[AllowedUser]) -> list[UserRecord]:
    admin_ids = {u.telegram_id for u in allowlist if u.is_admin}
    return [
        UserRecord(
            id=r["id"],
            telegram_id=r["telegram_id"],
            slug=r["slug"],
            display_name=r["display_name"],
            timezone=r["timezone"],
            is_admin=r["telegram_id"] in admin_ids,
        )
        for r in conn.execute("SELECT * FROM users WHERE enabled = 1 ORDER BY id")
    ]
