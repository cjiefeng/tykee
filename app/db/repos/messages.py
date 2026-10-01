"""messages table: every allowlisted message in the group/DMs plus the bot's replies."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Literal

Role = Literal["user", "assistant", "tool"]
Kind = Literal["text", "sticker", "photo", "emoji", "voice", "other"]


@dataclass(frozen=True)
class StoredMessage:
    id: int
    chat_id: int
    tg_message_id: int | None
    user_id: int | None
    role: Role
    kind: Kind
    text: str
    created_at: str


def text_content(text: str) -> str:
    return json.dumps([{"type": "text", "text": text}], ensure_ascii=False)


def _text_of(content: str) -> str:
    blocks = json.loads(content)
    return "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def insert(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    tg_message_id: int | None,
    user_id: int | None,
    role: Role,
    kind: Kind,
    content: str,
) -> int | None:
    """Returns the new row id, or None if this Telegram message was already stored."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO messages(chat_id, tg_message_id, user_id, role, kind, content) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (chat_id, tg_message_id, user_id, role, kind, content),
    )
    return cur.lastrowid if cur.rowcount else None


def recent(conn: sqlite3.Connection, chat_id: int, limit: int) -> list[StoredMessage]:
    """Last ``limit`` user/assistant rows for a chat, oldest first (tool rows are not replayed)."""
    rows = conn.execute(
        "SELECT * FROM messages WHERE chat_id = ? AND role IN ('user', 'assistant') "
        "ORDER BY id DESC LIMIT ?",
        (chat_id, limit),
    ).fetchall()
    return [
        StoredMessage(
            id=r["id"],
            chat_id=r["chat_id"],
            tg_message_id=r["tg_message_id"],
            user_id=r["user_id"],
            role=r["role"],
            kind=r["kind"],
            text=_text_of(r["content"]),
            created_at=r["created_at"],
        )
        for r in reversed(rows)
    ]
