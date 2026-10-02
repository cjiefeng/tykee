"""chat_summaries: one rolling summary per chat (§7.2)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Summary:
    text: str
    upto_msg_id: int


def get(conn: sqlite3.Connection, chat_id: int) -> Summary | None:
    r = conn.execute(
        "SELECT summary, upto_msg_id FROM chat_summaries WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    return Summary(r["summary"], r["upto_msg_id"]) if r else None


def upsert(conn: sqlite3.Connection, chat_id: int, text: str, upto_msg_id: int) -> None:
    conn.execute(
        "INSERT INTO chat_summaries(chat_id, summary, upto_msg_id) VALUES (?, ?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET summary = excluded.summary, "
        "upto_msg_id = excluded.upto_msg_id "
        "WHERE excluded.upto_msg_id > chat_summaries.upto_msg_id",
        (chat_id, text, upto_msg_id),
    )
