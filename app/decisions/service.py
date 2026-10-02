"""Async façade over the decision engine, shared by Claude's tools, commands, callbacks and the
fallback path. Every DB access goes through ``db.read`` / ``db.write``."""

from __future__ import annotations

import json
import random
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.db.database import Database
from app.db.repos.users import UserRecord
from app.decisions import categories as cats
from app.decisions import engine, feedback
from app.decisions.categories import Category, ResolveResult
from app.decisions.engine import PickRequest, PickResult
from app.decisions.feedback import Action, FeedbackResult
from app.settings import SettingsStore
from app.timeutil import from_sql, local_day_start, to_sql, utcnow


@dataclass(frozen=True)
class OptionInfo:
    name: str
    tags: list[str]
    owner: str
    base_weight: float


@dataclass(frozen=True)
class RecentDecision:
    created_at: datetime
    choice_text: str
    status: str
    for_users: str


class DecisionService:
    def __init__(
        self,
        *,
        db: Database,
        settings: SettingsStore,
        users: Sequence[UserRecord],
        clock: Callable[[], datetime] = utcnow,
        rng: random.Random | None = None,
    ) -> None:
        self._db = db
        self._settings = settings
        self._users_by_slug = {u.slug: u.id for u in users}
        self._clock = clock
        self._rng = rng

    @property
    def user_slugs(self) -> list[str]:
        return list(self._users_by_slug)

    async def resolve(
        self,
        *,
        phrase: str,
        proposed_slug: str,
        description: str,
        proposed_tau_days: float,
        use_existing: str | None = None,
        create_new: bool = False,
    ) -> ResolveResult:
        return await self._db.write(
            lambda c: cats.resolve(
                c,
                phrase=phrase,
                proposed_slug=proposed_slug,
                description=description,
                proposed_tau_days=proposed_tau_days,
                use_existing=use_existing,
                create_new=create_new,
            )
        )

    async def lookup(self, name: str) -> Category | None:
        return await self._db.read(lambda c: cats.lookup(c, name))

    async def match_in_text(self, text: str) -> Category | None:
        return await self._db.read(lambda c: cats.match_in_text(c, text))

    async def pick(
        self, category: Category, req: PickRequest, *, asked_by: int, chat_id: int | None
    ) -> PickResult:
        s = await self._settings.load()
        now = self._clock()

        def _run(conn: sqlite3.Connection) -> PickResult:
            current = cats.get_by_id(conn, category.id) or category
            return engine.pick(
                conn,
                current,
                req,
                users_by_slug=self._users_by_slug,
                asked_by=asked_by,
                chat_id=chat_id,
                now=now,
                session_hours=s.decisions_session_hours,
                rng=self._rng,
            )

        return await self._db.write(_run)

    async def feedback(self, decision_id: int, action: Action, user_id: int) -> FeedbackResult:
        return await self._db.write(lambda c: feedback.apply(c, decision_id, action, user_id))

    async def reroll(
        self, fb: FeedbackResult, *, asked_by: int, chat_id: int | None
    ) -> PickResult | None:
        """Re-run the original pick (one result). None if the category is gone."""
        if fb.request is None:
            return None
        category = await self._db.read(lambda c: cats.get_by_id(c, fb.category_id))
        if category is None:
            return None
        req = PickRequest(
            category_id=category.id,
            for_users=fb.request.for_users,
            n=1,
            include_tags=fb.request.include_tags,
            exclude_tags=fb.request.exclude_tags,
            extra_candidates=fb.request.extra_candidates,
        )
        return await self.pick(category, req, asked_by=asked_by, chat_id=chat_id)

    async def list_options(self, category: Category) -> list[OptionInfo]:
        rows = await self._db.read(
            lambda c: c.execute(
                "SELECT name, tags_json, owner, base_weight FROM options "
                "WHERE category_id = ? AND active = 1 ORDER BY name",
                (category.id,),
            ).fetchall()
        )
        return [
            OptionInfo(r["name"], json.loads(r["tags_json"]), r["owner"], r["base_weight"])
            for r in rows
        ]

    async def add_option(
        self, category: Category, name: str, tags: Sequence[str], owner: str
    ) -> bool:
        """Returns False if an option with that name already exists in the category."""

        def _add(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                "INSERT OR IGNORE INTO options(category_id, name, tags_json, owner, created_by) "
                "VALUES (?, ?, ?, ?, 'bot')",
                (category.id, name.strip(), json.dumps(list(tags), ensure_ascii=False), owner),
            )
            return cur.rowcount > 0

        return await self._db.write(_add)

    async def recent(self, category: Category, days: float) -> list[RecentDecision]:
        since = to_sql(self._clock() - timedelta(days=days))
        rows = await self._db.read(
            lambda c: c.execute(
                "SELECT created_at, choice_text, status, for_users FROM decisions "
                "WHERE category_id = ? AND created_at >= ? AND status != 'suggested' "
                "ORDER BY created_at DESC LIMIT 30",
                (category.id, since),
            ).fetchall()
        )
        return [
            RecentDecision(from_sql(r["created_at"]), r["choice_text"], r["status"], r["for_users"])
            for r in rows
        ]

    async def attach_message(
        self, decision_ids: Sequence[int], chat_id: int, tg_message_id: int
    ) -> None:
        if not decision_ids:
            return
        ids = list(decision_ids)
        await self._db.write(
            lambda c: c.execute(
                f"UPDATE decisions SET tg_message_id = ? "
                f"WHERE chat_id IS ? AND id IN ({','.join('?' * len(ids))})",
                (tg_message_id, chat_id, *ids),
            )
        )

    async def open_on_message(self, chat_id: int, tg_message_id: int) -> list[tuple[int, str]]:
        """Decisions still awaiting feedback on a bot message, for rebuilding its keyboard."""
        rows = await self._db.read(
            lambda c: c.execute(
                "SELECT id, choice_text FROM decisions WHERE chat_id IS ? AND tg_message_id = ? "
                "AND status = 'suggested' ORDER BY id",
                (chat_id, tg_message_id),
            ).fetchall()
        )
        return [(r["id"], r["choice_text"]) for r in rows]

    async def today(self, chat_id: int, tz: ZoneInfo) -> list[tuple[str, str, str]]:
        """(category, choice, status) for decisions made in this chat since local midnight."""
        since = to_sql(local_day_start(self._clock(), tz))
        rows = await self._db.read(
            lambda c: c.execute(
                "SELECT c.display_name, d.choice_text, d.status FROM decisions d "
                "JOIN categories c ON c.id = d.category_id "
                "WHERE d.chat_id IS ? AND d.created_at >= ? ORDER BY d.id",
                (chat_id, since),
            ).fetchall()
        )
        return [(r[0], r[1], r[2]) for r in rows]
