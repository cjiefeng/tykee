"""chat_state and ambient_log access (§10.2). Daily counters belong to ``state_day`` (household
TZ date); a different day means they read as reset."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from app.timeutil import from_sql, to_sql


@dataclass(frozen=True)
class ChatState:
    chat_id: int
    muted_until: datetime | None
    last_unprompted_at: datetime | None
    unprompted_today: int
    cooldown_multiplier: float


def _ensure(conn: sqlite3.Connection, chat_id: int) -> None:
    conn.execute("INSERT OR IGNORE INTO chat_state(chat_id) VALUES (?)", (chat_id,))


def load(conn: sqlite3.Connection, chat_id: int, today: str) -> ChatState:
    r = conn.execute("SELECT * FROM chat_state WHERE chat_id = ?", (chat_id,)).fetchone()
    if r is None:
        return ChatState(chat_id, None, None, 0, 1.0)
    same_day = r["state_day"] == today
    return ChatState(
        chat_id=chat_id,
        muted_until=from_sql(r["muted_until"]) if r["muted_until"] else None,
        last_unprompted_at=from_sql(r["last_unprompted_at"]) if r["last_unprompted_at"] else None,
        unprompted_today=r["unprompted_today"] if same_day else 0,
        cooldown_multiplier=r["cooldown_multiplier"] if same_day else 1.0,
    )


def record_unprompted(conn: sqlite3.Connection, chat_id: int, now: datetime, today: str) -> None:
    _ensure(conn, chat_id)
    conn.execute(
        "UPDATE chat_state SET last_unprompted_at = ?, "
        "unprompted_today = CASE WHEN state_day IS ? THEN unprompted_today + 1 ELSE 1 END, "
        "cooldown_multiplier = CASE WHEN state_day IS ? THEN cooldown_multiplier ELSE 1.0 END, "
        "state_day = ? WHERE chat_id = ?",
        (to_sql(now), today, today, today, chat_id),
    )


def double_cooldown(conn: sqlite3.Connection, chat_id: int, today: str) -> None:
    _ensure(conn, chat_id)
    conn.execute(
        "UPDATE chat_state SET "
        "cooldown_multiplier = CASE WHEN state_day IS ? THEN cooldown_multiplier * 2 ELSE 2.0 END, "
        "unprompted_today = CASE WHEN state_day IS ? THEN unprompted_today ELSE 0 END, "
        "state_day = ? WHERE chat_id = ?",
        (today, today, today, chat_id),
    )


def set_mute(conn: sqlite3.Connection, chat_id: int, until: datetime | None) -> None:
    _ensure(conn, chat_id)
    conn.execute(
        "UPDATE chat_state SET muted_until = ? WHERE chat_id = ?",
        (to_sql(until) if until else None, chat_id),
    )


def log(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    from_msg_id: int,
    to_msg_id: int,
    action: str,
    now: datetime,
    rule: str | None = None,
    reason: str | None = None,
    confidence: float | None = None,
    reply_tg_message_id: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO ambient_log(chat_id, from_msg_id, to_msg_id, action, rule, reason, "
        "confidence, reply_tg_message_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            chat_id,
            from_msg_id,
            to_msg_id,
            action,
            rule,
            reason,
            confidence,
            reply_tg_message_id,
            to_sql(now),
        ),
    )
    return int(cur.lastrowid or 0)


def unprompted_by_message(conn: sqlite3.Connection, chat_id: int, tg_message_id: int) -> int | None:
    r = conn.execute(
        "SELECT id FROM ambient_log WHERE chat_id = ? AND action = 'respond' "
        "AND reply_tg_message_id = ?",
        (chat_id, tg_message_id),
    ).fetchone()
    return int(r["id"]) if r else None


def latest_unprompted_since(conn: sqlite3.Connection, chat_id: int, since: datetime) -> int | None:
    r = conn.execute(
        "SELECT id FROM ambient_log WHERE chat_id = ? AND action = 'respond' AND created_at >= ? "
        "ORDER BY id DESC LIMIT 1",
        (chat_id, to_sql(since)),
    ).fetchone()
    return int(r["id"]) if r else None


def set_feedback(conn: sqlite3.Connection, log_id: int, feedback: str) -> bool:
    """Returns True if the feedback changed (so effects like doubling apply once)."""
    cur = conn.execute(
        "UPDATE ambient_log SET feedback = ? WHERE id = ? AND feedback IS NOT ?",
        (feedback, log_id, feedback),
    )
    return cur.rowcount > 0
