"""messages table: every allowlisted message in the group/DMs plus the bot's replies, and
(§10.7) messages the account reader fetched from allowlisted chats.

``source`` separates the two: from Jack's account a DM's peer id is the other person's user id,
which is also the bot's chat_id with them. Every query on messages by chat_id filters by source
(a test greps ``app/`` for it)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Literal

Role = Literal["user", "assistant", "tool"]
Kind = Literal["text", "sticker", "photo", "emoji", "voice", "other"]
Source = Literal["bot", "account_reader"]
BOT: Source = "bot"
READER: Source = "account_reader"


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
    thread_id: int | None = None


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
    thread_id: int | None = None,
    source: Source = BOT,
    created_at: str | None = None,
) -> int | None:
    """Returns the new row id, or None if this Telegram message was already stored."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO messages(chat_id, tg_message_id, user_id, role, kind, content, "
        "thread_id, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
        "COALESCE(?, datetime('now')))",
        (chat_id, tg_message_id, user_id, role, kind, content, thread_id, source, created_at),
    )
    return cur.lastrowid if cur.rowcount else None


def _thread_clause(only_thread: int | None) -> tuple[str, tuple[int, ...]]:
    return ("AND thread_id = ? ", (only_thread,)) if only_thread is not None else ("", ())


def recent(
    conn: sqlite3.Connection,
    chat_id: int,
    limit: int,
    only_thread: int | None = None,
    source: Source = BOT,
) -> list[StoredMessage]:
    """Last ``limit`` user/assistant rows for a chat, oldest first (tool rows are not replayed).
    ``only_thread`` limits a forum group to one topic (§10.4: the answer topic)."""
    clause, args = _thread_clause(only_thread)
    rows = conn.execute(
        "SELECT * FROM messages WHERE source = ? AND chat_id = ? "
        f"AND role IN ('user', 'assistant') {clause}ORDER BY id DESC LIMIT ?",
        (source, chat_id, *args, limit),
    ).fetchall()
    return [from_row(r) for r in reversed(rows)]


def from_row(r: sqlite3.Row) -> StoredMessage:
    return StoredMessage(
        id=r["id"],
        chat_id=r["chat_id"],
        tg_message_id=r["tg_message_id"],
        user_id=r["user_id"],
        role=r["role"],
        kind=r["kind"],
        text=_text_of(r["content"]),
        created_at=r["created_at"],
        thread_id=r["thread_id"],
    )


def after(
    conn: sqlite3.Connection,
    chat_id: int,
    after_id: int,
    only_thread: int | None = None,
    source: Source = BOT,
) -> list[StoredMessage]:
    """User/assistant rows with id > ``after_id``, oldest first (for summarisation)."""
    clause, args = _thread_clause(only_thread)
    rows = conn.execute(
        "SELECT * FROM messages WHERE source = ? AND chat_id = ? AND id > ? "
        f"AND role IN ('user', 'assistant') {clause}ORDER BY id",
        (source, chat_id, after_id, *args),
    ).fetchall()
    return [from_row(r) for r in rows]
