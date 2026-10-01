"""The one allowed group chat (§10). Env ``GROUP_CHAT_ID`` seeds it; a group→supergroup
upgrade changes Telegram's chat id, so the new id is persisted in settings together with the
id it replaced. On restart a stale env value that was migrated away is mapped to the new id."""

from __future__ import annotations

import logging
import sqlite3

from app.db.database import Database
from app.settings import get_value, set_value

log = logging.getLogger(__name__)

KEY_ID = "telegram.group_chat_id"
KEY_MIGRATED_FROM = "telegram.group_migrated_from"


def resolve_group_id(conn: sqlite3.Connection, env_group_id: int | None) -> int | None:
    stored = get_value(conn, KEY_ID)
    migrated_from = get_value(conn, KEY_MIGRATED_FROM)
    if env_group_id is None:
        return int(stored) if stored is not None else None
    if migrated_from is not None and int(migrated_from) == env_group_id and stored is not None:
        log.warning(
            "GROUP_CHAT_ID is stale (group was upgraded); update .env",
            extra={"env": env_group_id, "current": stored},
        )
        return int(stored)
    if stored != env_group_id:
        set_value(conn, KEY_ID, env_group_id)
    return env_group_id


class GroupRegistry:
    def __init__(self, db: Database, group_id: int | None) -> None:
        self._db = db
        self.group_id = group_id

    def is_allowed(self, chat_id: int) -> bool:
        return self.group_id is not None and chat_id == self.group_id

    async def migrate(self, old_id: int, new_id: int) -> bool:
        """Follow a group→supergroup upgrade. Returns True if the configured group moved."""
        if self.group_id != old_id:
            return False

        def _save(conn: sqlite3.Connection) -> None:
            set_value(conn, KEY_ID, new_id)
            set_value(conn, KEY_MIGRATED_FROM, old_id)

        await self._db.write(_save)
        self.group_id = new_id
        log.warning(
            "group migrated to supergroup; set GROUP_CHAT_ID in .env to the new id",
            extra={"old": old_id, "new": new_id},
        )
        return True
