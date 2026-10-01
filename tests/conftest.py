from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiogram.types import Chat, Message, MessageEntity, User

from app.config import parse_allowlist
from app.db.database import Database
from app.db.migrate import apply_migrations
from app.db.repos.users import UserRecord, load_enabled, upsert_allowlist
from app.settings import SettingsStore, seed_settings
from app.telegram.addressing import BotIdentity

JACK_TG = 111
PARTNER_TG = 222
STRANGER_TG = 999
GROUP_ID = -100500
BOT = BotIdentity(id=777, username="TykeeBot")
ALLOWLIST = parse_allowlist(f"{JACK_TG}:jack,{PARTNER_TG}:partner")


@dataclass
class Env:
    db: Database
    users: list[UserRecord]
    settings: SettingsStore

    @property
    def jack(self) -> UserRecord:
        return self.users[0]

    @property
    def partner(self) -> UserRecord:
        return self.users[1]


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    db = Database(tmp_path / "bot.db")
    await db.open()
    await db.run_raw(apply_migrations)

    def _seed(conn: sqlite3.Connection) -> list[UserRecord]:
        seed_settings(conn)
        upsert_allowlist(conn, ALLOWLIST, "Asia/Singapore")
        return load_enabled(conn, ALLOWLIST)

    users = await db.write(_seed)
    yield Env(db=db, users=users, settings=SettingsStore(db))
    await db.close()


_msg_id = 0


def tg_user(uid: int, name: str = "U", is_bot: bool = False) -> User:
    return User(id=uid, is_bot=is_bot, first_name=name)


def tg_message(
    text: str | None = "hello",
    *,
    from_id: int = JACK_TG,
    chat_id: int = GROUP_ID,
    chat_type: str = "supergroup",
    entities: list[MessageEntity] | None = None,
    reply_to: Message | None = None,
    message_id: int | None = None,
    **extra: object,
) -> Message:
    global _msg_id
    _msg_id += 1
    return Message(
        message_id=message_id if message_id is not None else _msg_id,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type=chat_type),
        from_user=tg_user(from_id),
        text=text,
        entities=entities,
        reply_to_message=reply_to,
        **extra,  # type: ignore[arg-type]
    )


def mention(text: str, handle: str = "@TykeeBot") -> tuple[str, list[MessageEntity]]:
    full = f"{handle} {text}"
    return full, [MessageEntity(type="mention", offset=0, length=len(handle))]
