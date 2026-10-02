"""Scheduled nudges (§10.3): at a set time Tykee starts the conversation with a weighted pick,
e.g. "🎲 Dinner? I'm thinking **Thai** (last time was 9 days ago)".

Off by default. The schedule lives in settings (`nudges.items`, hot-reloaded); the scheduler
ticks every minute and a nudge fires when its time falls inside the last `nudges.grace_min`
minutes, at most once per household day (`nudge_runs`). No Claude call: the pick comes from the
decision engine, so nudges cost nothing and still work when the budget is exhausted.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from app.ambient import state as ambient_state
from app.db.database import Database
from app.db.repos.users import UserRecord
from app.decisions.engine import PickRequest
from app.decisions.service import DecisionService
from app.settings import DAYS, Nudge, SettingsStore
from app.timeutil import from_sql, local_day_start, to_sql, utcnow

log = logging.getLogger(__name__)


class NudgeSender(Protocol):
    async def send_nudge(
        self, chat_id: int, text: str, picks: Sequence[tuple[int, str]], *, group: bool
    ) -> list[int]:
        """Send (into the answer topic when ``group``) and store it in history. Returns the
        sent message ids; empty if the send failed."""
        ...


@dataclass(frozen=True)
class NudgeOutcome:
    nudge_id: str
    status: str  # 'sent' | 'skipped' | 'failed'
    reason: str = ""
    decision_id: int | None = None


def is_due(nudge: Nudge, local_now: datetime, grace_min: float) -> bool:
    """True when today is one of the nudge's days and its time was within the last
    ``grace_min`` minutes (so a restart shortly after the time still sends it)."""
    if DAYS[local_now.weekday()] not in nudge.days:
        return False
    hh, mm = (int(p) for p in nudge.time.split(":"))
    at = datetime.combine(local_now.date(), time(hh, mm), tzinfo=local_now.tzinfo)
    return timedelta(0) <= local_now - at < timedelta(minutes=grace_min)


def nudge_text(category: str, choice: str, days_since: int | None) -> str:
    text = f"🎲 {category}? I'm thinking **{choice}**"
    if days_since is not None and days_since >= 2:
        text += f" (last time was {days_since} days ago)"
    return text


def _decided_today(conn: sqlite3.Connection, category_id: int, chat_id: int, since: str) -> bool:
    """A pick that's pending or accepted today (asked for, nudged or observed) counts."""
    row = conn.execute(
        "SELECT 1 FROM decisions WHERE category_id = ? AND created_at >= ? "
        "AND status IN ('suggested', 'accepted') AND (chat_id = ? OR source = 'observed') "
        "LIMIT 1",
        (category_id, since, chat_id),
    ).fetchone()
    return row is not None


def _last_chosen(conn: sqlite3.Connection, decision_id: int) -> datetime | None:
    """When the same option was last accepted in this category, before this decision."""
    row = conn.execute(
        "SELECT MAX(prev.created_at) FROM decisions d JOIN decisions prev "
        "ON prev.category_id = d.category_id AND prev.id != d.id AND prev.status = 'accepted' "
        "AND (prev.option_id = d.option_id OR lower(prev.choice_text) = lower(d.choice_text)) "
        "WHERE d.id = ?",
        (decision_id,),
    ).fetchone()
    return from_sql(row[0]) if row and row[0] else None


def _record(conn: sqlite3.Connection, day: str, o: NudgeOutcome, now: datetime) -> None:
    conn.execute(
        "INSERT INTO nudge_runs(nudge_id, day, status, reason, decision_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(nudge_id, day) DO UPDATE SET "
        "status = excluded.status, reason = excluded.reason, "
        "decision_id = excluded.decision_id, created_at = excluded.created_at",
        (o.nudge_id, day, o.status, o.reason or None, o.decision_id, to_sql(now)),
    )


class NudgeService:
    def __init__(
        self,
        *,
        db: Database,
        settings: SettingsStore,
        decisions: DecisionService,
        sender: NudgeSender,
        users: Sequence[UserRecord],
        tz: ZoneInfo,
        group_id: Callable[[], int | None],
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._settings = settings
        self._decisions = decisions
        self._sender = sender
        self._users = list(users)
        self._tz = tz
        self._group_id = group_id
        self._clock = clock

    async def tick(self) -> list[NudgeOutcome]:
        s = await self._settings.load()
        if not s.nudges_enabled:
            return []
        now = self._clock()
        local = now.astimezone(self._tz)
        day = local.date().isoformat()
        done = await self._db.read(
            lambda c: {
                r[0] for r in c.execute("SELECT nudge_id FROM nudge_runs WHERE day = ?", (day,))
            }
        )
        outcomes = []
        for nudge in s.nudges_items:
            if nudge.enabled and nudge.id not in done and is_due(nudge, local, s.nudges_grace_min):
                outcomes.append(await self._run(nudge, now, force=False))
        return outcomes

    async def run_now(self, nudge_id: str) -> NudgeOutcome:
        """Dashboard "Send now": ignores the schedule, the master toggle and the skip rules (it's
        a test), and counts as today's run so the scheduled one doesn't repeat it."""
        s = await self._settings.load()
        nudge = next((n for n in s.nudges_items if n.id == nudge_id), None)
        if nudge is None:
            return NudgeOutcome(nudge_id, "failed", "no such nudge")
        return await self._run(nudge, self._clock(), force=True)

    async def _run(self, nudge: Nudge, now: datetime, *, force: bool) -> NudgeOutcome:
        outcome = await self._attempt(nudge, now, force=force)
        day = now.astimezone(self._tz).date().isoformat()
        await self._db.write(lambda c: _record(c, day, outcome, now))
        log.info(
            "nudge",
            extra={"nudge_id": nudge.id, "status": outcome.status, "reason": outcome.reason},
        )
        return outcome

    async def _attempt(self, nudge: Nudge, now: datetime, *, force: bool) -> NudgeOutcome:
        def skip(reason: str) -> NudgeOutcome:
            return NudgeOutcome(nudge.id, "skipped", reason)

        is_group = nudge.target == "group"
        admin = next((u for u in self._users if u.is_admin), self._users[0])
        chat_id: int
        if is_group:
            group = self._group_id()
            if group is None:
                return skip("no group configured")
            chat_id, asker, for_users = group, admin, "both"
        else:
            user = next((u for u in self._users if u.slug == nudge.target), None)
            if user is None:
                return skip(f"unknown target {nudge.target!r}")
            chat_id, asker, for_users = user.telegram_id, user, user.slug
        category = await self._decisions.lookup(nudge.category)
        if category is None:
            return skip(f"unknown category {nudge.category!r}")
        if not force:
            today = now.astimezone(self._tz).date().isoformat()
            since = to_sql(local_day_start(now, self._tz))

            def _check(c: sqlite3.Connection) -> str | None:
                if is_group:
                    muted = ambient_state.load(c, chat_id, today).muted_until
                    if muted is not None and muted > now:
                        return "group muted"
                if _decided_today(c, category.id, chat_id, since):
                    return f"{category.display_name} already decided today"
                return None

            reason = await self._db.read(_check)
            if reason:
                return skip(reason)
        req = PickRequest(category_id=category.id, for_users=for_users, n=1)
        result = await self._decisions.pick(category, req, asked_by=asker.id, chat_id=chat_id)
        if not result.picks:
            return skip(f"no options for {category.display_name}")
        pick = result.picks[0]
        last = await self._db.read(lambda c: _last_chosen(c, pick.decision_id))
        days_since = (now - last).days if last else None
        text = nudge_text(category.display_name, pick.name, days_since)
        try:
            ids = await self._sender.send_nudge(
                chat_id, text, [(pick.decision_id, pick.name)], group=is_group
            )
        except Exception as e:
            log.exception("nudge send failed", extra={"nudge_id": nudge.id})
            return NudgeOutcome(nudge.id, "failed", str(e)[:200], pick.decision_id)
        if not ids:
            return NudgeOutcome(nudge.id, "failed", "send failed", pick.decision_id)
        return NudgeOutcome(nudge.id, "sent", decision_id=pick.decision_id)
