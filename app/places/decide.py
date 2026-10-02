"""Shared places → decisions (§10.5), code only, no LLM call.

In the answer topic, a message that shares a named place and says "eating here" (or an intent
phrase within ``places.intent_window_s`` before or after it) is recorded as an accepted
decision with ``source='user'``. The category is the one the message names ("dinner here") or
the household-time meal slot's. Tykee confirms with a reaction instead of a reply, keeping §10.2
"silent by default". Without intent, the place is offered as an option in the memory inbox.
Mentioned messages and DMs go to Claude instead, which uses the ``record_decision`` tool; its
recorded places get the same reaction via ``confirm``.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from app.brain.memory import MemoryService
from app.db.database import Database
from app.db.repos import messages as messages_repo
from app.db.repos.users import UserRecord
from app.decisions.categories import Category
from app.decisions.service import DecisionService
from app.places import links
from app.places.intent import has_intent, slot_category
from app.places.service import AnnotatedText, Place, PlaceService
from app.settings import RuntimeSettings, SettingsStore
from app.timeutil import utcnow

log = logging.getLogger(__name__)

React = Callable[[int, int, str], Awaitable[None]]  # chat_id, message_id, emoji


@dataclass(frozen=True)
class _Prior:
    tg_message_id: int | None
    text: str


class SharedPlaces:
    def __init__(
        self,
        *,
        db: Database,
        settings: SettingsStore,
        places: PlaceService,
        decisions: DecisionService,
        react: React,
        tz: ZoneInfo,
        memory: MemoryService | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._settings = settings
        self.places = places
        self._decisions = decisions
        self._react = react
        self._tz = tz
        self._memory = memory
        self._clock = clock

    async def on_chatter(
        self,
        *,
        chat_id: int,
        thread: int | None,
        row_id: int,
        tg_message_id: int,
        actor: UserRecord,
        text: str,
        found: AnnotatedText,
    ) -> bool:
        """An answer-topic group message that didn't address Tykee. True if a decision was
        recorded (and reacted to)."""
        s = await self._settings.load()
        if not s.places_enabled:
            return False
        phrases = s.places_intent_phrases
        if found.places:
            place = found.places[-1]
            intent = has_intent(text, phrases)
            context = text
            if not intent:
                before = await self._recent(chat_id, thread, row_id, s)
                said = [p for p in before if has_intent(p.text, phrases) and "⟦" not in p.text]
                intent = bool(said)
                context = "\n".join([*(p.text for p in said), text])
            if intent:
                return await self._record(chat_id, actor, context, place, tg_message_id, s)
            await self._suggest(chat_id, text, place, s)
            return False
        if found.unnamed or not has_intent(text, phrases):
            return False
        # "eating here" right after someone shared a place.
        for prior in await self._recent(chat_id, thread, row_id, s):
            marked = [a for a in links.annotations_in(prior.text) if a.place_id is not None]
            if not marked or marked[-1].place_id is None:
                continue
            shared = await self.places.get(marked[-1].place_id)
            if shared is None or prior.tg_message_id is None:
                return False
            context = f"{prior.text}\n{text}"
            return await self._record(chat_id, actor, context, shared, prior.tg_message_id, s)
        return False

    async def confirm(
        self, chat_id: int, recorded: Sequence[tuple[int, int | None]], fallback: int | None
    ) -> None:
        """React to the messages that shared the places Claude just recorded (``fallback``, the
        message Claude answered, when there's no place)."""
        if not recorded:
            return
        s = await self._settings.load()
        done: set[int] = set()
        for _, place_id in recorded:
            target = await self._message_with(chat_id, place_id) if place_id else None
            target = target or fallback
            if target is not None and target not in done:
                done.add(target)
                await self._react(chat_id, target, s.places_reaction)

    # --- helpers -----------------------------------------------------------------------------

    async def _recent(
        self, chat_id: int, thread: int | None, row_id: int, s: RuntimeSettings
    ) -> list[_Prior]:
        """User messages in this topic within the intent window before ``row_id``, newest
        first. Uses SQLite's clock, the same one that stamped the rows."""
        window = f"-{s.places_intent_window_s} seconds"

        def _q(c: sqlite3.Connection) -> list[_Prior]:
            rows = c.execute(
                "SELECT * FROM messages WHERE source = 'bot' AND chat_id = ? AND thread_id IS ? "
                "AND role = 'user' "
                "AND id < ? AND created_at >= datetime('now', ?) ORDER BY id DESC LIMIT 5",
                (chat_id, thread, row_id, window),
            ).fetchall()
            return [_Prior(r["tg_message_id"], messages_repo.from_row(r).text) for r in rows]

        return await self._db.read(_q)

    async def _message_with(self, chat_id: int, place_id: int) -> int | None:
        needle = f"place_id={place_id}⟧"
        row = await self._db.read(
            lambda c: c.execute(
                "SELECT tg_message_id FROM (SELECT id, tg_message_id, content FROM messages "
                "WHERE source = 'bot' AND chat_id = ? AND role = 'user' ORDER BY id DESC LIMIT 50) "
                "WHERE instr(content, ?) > 0 ORDER BY id DESC LIMIT 1",
                (chat_id, needle),
            ).fetchone()
        )
        return int(row[0]) if row and row[0] is not None else None

    async def _category(self, text: str, s: RuntimeSettings) -> Category | None:
        named = await self._decisions.match_in_text(links.strip_markup(text))
        if named is not None:
            return named
        slug = slot_category(self._clock().astimezone(self._tz).time(), s.places_meal_slots)
        return await self._decisions.lookup(slug) if slug else None

    async def _record(
        self,
        chat_id: int,
        actor: UserRecord,
        text: str,
        place: Place,
        tg_message_id: int,
        s: RuntimeSettings,
    ) -> bool:
        category = await self._category(text, s)
        if category is None:
            log.info("shared place: no category", extra={"place_id": place.id})
            return False
        r = await self._decisions.record_user(
            category,
            choice=place.name,
            for_users="both",
            asked_by=actor.id,
            chat_id=chat_id,
            place_id=place.id,
        )
        if not r.created:
            return False
        await self.places.visit(place.id)
        if self._memory is not None:
            now = self._clock().astimezone(self._tz)
            await self._memory.log_decision(
                f"- {now:%H:%M} · {category.display_name} · **{place.name}** · for both"
                f" · 📍 by {actor.display_name}"
            )
        log.info("shared place recorded", extra={"place_id": place.id, "slug": category.slug})
        await self._react(chat_id, tg_message_id, s.places_reaction)
        return True

    async def _suggest(self, chat_id: int, text: str, place: Place, s: RuntimeSettings) -> None:
        """A place shared without intent: offer it as an option (inbox), once."""
        if self._memory is None:
            return
        category = await self._category(text, s)
        if category is None:
            return
        known = await self._db.read(
            lambda c: c.execute(
                "SELECT 1 FROM options WHERE category_id = ? AND (place_id = ? OR "
                "lower(name) = lower(?))",
                (category.id, place.id, place.name),
            ).fetchone()
        )
        content = f"New {category.display_name.lower()} option: {place.name}"
        pending = await self._db.read(
            lambda c: c.execute(
                "SELECT 1 FROM memory_inbox WHERE kind = 'option' AND status = 'pending' "
                "AND content = ?",
                (content,),
            ).fetchone()
        )
        if known or pending:
            return
        await self._memory.suggest(
            kind="option",
            content=content,
            reason="Shared in the chat",
            source=f"telegram:{chat_id}",
            payload={
                "category_id": category.id,
                "name": place.name,
                "tags": ["place"],
                "place_id": place.id,
            },
        )
