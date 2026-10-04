"""Forum topics (§10.4): read every topic, answer only in one.

Thread ids: a message in a topic carries ``message_thread_id`` (with ``is_topic_message``);
in a forum group, everything else is the General topic, stored as ``1``. Non-forum groups and
DMs have no thread (``None``). Sends to General must omit ``message_thread_id``.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from aiogram.types import Message

from app.db.database import Database
from app.settings import SettingsStore, get_value, set_value
from app.timeutil import to_sql, utcnow

log = logging.getLogger(__name__)

GENERAL_THREAD = 1
KEY_ANSWER = "telegram.answer_topic_id"
GROUP_TYPES = {"group", "supergroup"}

# "own": the answer topic is a dedicated topic (not General) and `telegram.answer_topic_mode` is
# `addressed`, so every text message there counts as addressed to Tykee.
Gate = Literal["drop", "answer", "own", "offtopic"]


def thread_of(msg: Message) -> int | None:
    if msg.chat.type not in GROUP_TYPES:
        return None
    if msg.is_topic_message and msg.message_thread_id:
        return int(msg.message_thread_id)
    return GENERAL_THREAD if msg.chat.is_forum else None


def is_topic_error(message: str) -> bool:
    """Telegram's error when sending into a deleted ("message thread not found") or closed
    ("TOPIC_CLOSED") topic."""
    lowered = message.lower()
    return "thread" in lowered or "topic" in lowered


def send_thread(thread_id: int | None) -> int | None:
    """What to pass as ``message_thread_id`` when sending into ``thread_id``."""
    return None if thread_id in (None, GENERAL_THREAD) else thread_id


@dataclass(frozen=True)
class TopicInfo:
    thread_id: int
    name: str
    closed: bool
    messages: int
    last_activity: str | None


def seed_answer_topic(conn: sqlite3.Connection, env_topic: int | None) -> None:
    """Optional ``GROUP_TOPIC_ID`` seeds the answer topic once; the dashboard owns it after."""
    exists = conn.execute("SELECT 1 FROM settings WHERE key = ?", (KEY_ANSWER,)).fetchone()
    if exists is None and env_topic is not None:
        set_value(conn, KEY_ANSWER, env_topic)


class TopicService:
    def __init__(
        self,
        *,
        db: Database,
        settings: SettingsStore,
        group_id: Callable[[], int | None],
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._group_id = group_id
        self._db = db
        self._settings = settings
        self._clock = clock
        self._redirected: dict[tuple[int, int | None], date] = {}

    async def answer_topic(self) -> int | None:
        return (await self._settings.load()).telegram_answer_topic_id

    async def history_thread(self, chat_id: int) -> int | None:
        """Thread filter for a chat's conversation history: the answer topic in the group,
        nothing (whole chat) for DMs or when no answer topic is set."""
        if chat_id != self._group_id():
            return None
        return await self.answer_topic()

    async def outbound_thread(self) -> int | None:
        """``message_thread_id`` for any group send without an incoming message to reply to."""
        return send_thread(await self.answer_topic())

    async def gate(self, thread_id: int | None) -> Gate:
        s = await self._settings.load()
        if thread_id is not None and thread_id in s.telegram_ignored_topic_ids:
            return "drop"
        answer = s.telegram_answer_topic_id
        if answer is None or thread_id is None:
            return "answer"
        if thread_id == answer:
            own = s.telegram_answer_topic_mode == "addressed" and answer != GENERAL_THREAD
            return "own" if own else "answer"
        return "offtopic"

    async def off_topic_mode(self) -> str:
        return (await self._settings.load()).telegram_off_topic_mention

    def may_redirect(self, chat_id: int, thread_id: int | None) -> bool:
        """'Ask me in #Tykee' at most once per topic per day."""
        today = self._clock().date()
        key = (chat_id, thread_id)
        if self._redirected.get(key) == today:
            return False
        self._redirected[key] = today
        return True

    async def set_answer_topic(self, thread_id: int | None) -> None:
        """Move Tykee. The old answer topic was answered live, so its harvest cursor jumps to
        its newest message: the harvester only picks up what's said there from now on."""
        s = await self._settings.load()
        if thread_id is not None and thread_id in s.telegram_ignored_topic_ids:
            raise ValueError("that topic is ignored; un-ignore it first")
        old, group = s.telegram_answer_topic_id, self._group_id()

        def _set(c: sqlite3.Connection) -> None:
            set_value(c, KEY_ANSWER, thread_id)
            if old is None or old == thread_id or group is None:
                return
            c.execute(
                "INSERT INTO topic_harvest(chat_id, thread_id, last_msg_id) "
                "SELECT ?, ?, COALESCE(MAX(id), 0) FROM messages "
                "WHERE source = 'bot' AND chat_id = ? AND thread_id = ? "
                "ON CONFLICT(chat_id, thread_id) DO UPDATE SET "
                "last_msg_id = MAX(topic_harvest.last_msg_id, excluded.last_msg_id)",
                (group, old, group, old),
            )

        await self._db.write(_set)
        log.info("answer topic set", extra={"thread_id": thread_id})

    # --- topic names -------------------------------------------------------------------------

    async def seen(
        self,
        chat_id: int,
        thread_id: int,
        *,
        name: str | None = None,
        closed: bool | None = None,
    ) -> None:
        now = to_sql(self._clock())

        def _up(c: sqlite3.Connection) -> None:
            c.execute(
                "INSERT INTO forum_topics(chat_id, thread_id, name, closed, last_seen_at) "
                "VALUES (?, ?, ?, COALESCE(?, 0), ?) ON CONFLICT(chat_id, thread_id) DO UPDATE SET "
                "name = COALESCE(excluded.name, forum_topics.name), "
                "closed = COALESCE(?, forum_topics.closed), last_seen_at = excluded.last_seen_at",
                (
                    chat_id,
                    thread_id,
                    name,
                    None if closed is None else int(closed),
                    now,
                    None if closed is None else int(closed),
                ),
            )

        await self._db.write(_up)

    async def learn_from(self, msg: Message) -> bool:
        """Topic service messages update ``forum_topics``. Returns True if ``msg`` was one (and
        so isn't chat content to store)."""
        thread = thread_of(msg)
        if thread is None:
            return False
        chat_id = msg.chat.id
        if msg.forum_topic_created is not None:
            await self.seen(chat_id, thread, name=msg.forum_topic_created.name, closed=False)
            return True
        if msg.forum_topic_edited is not None:
            await self.seen(chat_id, thread, name=msg.forum_topic_edited.name)
            return True
        if msg.forum_topic_closed is not None:
            await self.seen(chat_id, thread, closed=True)
            return True
        if msg.forum_topic_reopened is not None:
            await self.seen(chat_id, thread, closed=False)
            return True
        # A topic message may reference the topic's creation message; learn the name from it.
        ref = msg.reply_to_message
        name = ref.forum_topic_created.name if ref and ref.forum_topic_created else None
        if thread == GENERAL_THREAD and name is None:
            name = "General"
        await self.seen(chat_id, thread, name=name)
        return False

    async def label(self, chat_id: int, thread_id: int, name: str) -> None:
        await self.seen(chat_id, thread_id, name=name.strip() or None)

    async def known(self, chat_id: int) -> list[TopicInfo]:
        rows = await self._db.read(
            lambda c: c.execute(
                "SELECT t.thread_id, t.name, t.closed, "
                "(SELECT COUNT(*) FROM messages m WHERE m.source = 'bot' "
                " AND m.chat_id = t.chat_id AND m.thread_id = t.thread_id) AS n, "
                "(SELECT MAX(created_at) FROM messages m WHERE m.source = 'bot' "
                " AND m.chat_id = t.chat_id AND m.thread_id = t.thread_id) AS last "
                "FROM forum_topics t WHERE t.chat_id = ? ORDER BY t.thread_id",
                (chat_id,),
            ).fetchall()
        )
        return [TopicInfo(r[0], r[1] or f"Topic {r[0]}", bool(r[2]), int(r[3]), r[4]) for r in rows]

    async def name_of(self, chat_id: int, thread_id: int | None) -> str:
        if thread_id is None:
            return "this chat"
        row = await self._db.read(
            lambda c: c.execute(
                "SELECT name FROM forum_topics WHERE chat_id = ? AND thread_id = ?",
                (chat_id, thread_id),
            ).fetchone()
        )
        return str(row[0]) if row and row[0] else f"Topic {thread_id}"


def stored_answer_topic(conn: sqlite3.Connection) -> int | None:
    value = get_value(conn, KEY_ANSWER)
    return int(value) if value is not None else None
