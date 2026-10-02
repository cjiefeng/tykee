"""Memory harvester (§10.4): turn chat in non-answer topics into memory, in small batches.

Stage 1, the tick: pure code, one indexed query comparing each topic's newest message with its
cursor. Nothing new → done, zero cost. Stage 2, only for topics with enough new chat: one
Haiku-tier extraction call per window (shared schema with the M5 import, safe-topic rules of
§15.4). Facts go to the inbox, chosen decisions in known categories are recorded as
``source='observed'`` (feeding recency), unknown categories and new options become inbox
suggestions. The cursor only advances after a successful harvest (or a window the model keeps
returning invalid output for, which is skipped after a few tries).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.brain.memory import MemoryPolicyError, MemoryService
from app.db.database import Database
from app.db.repos.messages import StoredMessage
from app.db.repos.users import UserRecord
from app.decisions.service import DecisionService
from app.extraction.schema import EXTRACTION_SCHEMA, SAFE_TOPIC_RULES, Extraction
from app.health import HealthState
from app.llm.client import (
    BudgetExceeded,
    LLMClient,
    LLMError,
    LLMRequest,
    budget_status,
)
from app.places import links as place_links
from app.places.service import PlaceService
from app.settings import RuntimeSettings, SettingsStore
from app.telegram.topics import TopicService
from app.timeutil import from_sql, to_sql, utcnow

log = logging.getLogger(__name__)

NO_TOPIC = 0  # pre-M4 rows without a thread_id are harvested as one "unknown topic"
MIN_FACT_CONFIDENCE = 0.6
WINDOW_GAP = timedelta(hours=2)
WINDOW_MAX_CHARS = 20_000  # ≈ 6k tokens, same order as the import windows (§15.3)
HARVEST_MAX_TOKENS = 3000
MAX_BAD_OUTPUT = 3  # invalid model output on the same window this many times → skip the window
_FACT_FILES = {
    "preference": "preferences",
    "place": "places",
    "fact": "facts",
    "constraint": "constraints",
}

BACKLOG_SQL = """
SELECT COALESCE(m.thread_id, 0) AS thread_id,
       COUNT(*)                 AS new_msgs,
       MIN(m.created_at)        AS oldest_new,
       MAX(m.id)                AS newest_id
FROM messages m
LEFT JOIN topic_harvest h
       ON h.chat_id = m.chat_id AND h.thread_id = COALESCE(m.thread_id, 0)
WHERE m.chat_id = ?
  AND m.role = 'user'
  AND COALESCE(m.thread_id, 0) <> ?
  AND m.id > COALESCE(h.last_msg_id, 0)
GROUP BY COALESCE(m.thread_id, 0)
"""


@dataclass(frozen=True)
class Backlog:
    thread_id: int  # NO_TOPIC for rows without a thread
    new_msgs: int
    oldest_new: datetime
    newest_id: int


@dataclass
class RunResult:
    thread_id: int
    status: str  # 'done' | 'error' | 'budget' | 'skipped'
    messages: int = 0
    facts: int = 0
    decisions: int = 0
    suggestions: int = 0
    skipped_out_of_scope: int = 0
    errors: list[str] = field(default_factory=list)


SYSTEM_PROMPT = """\
You read a couple's group-chat topic and extract what their decision bot, Tykee, should \
remember. Users (use these slugs as owner / for_users): {users}. Use "shared" for things about \
both of them or the household, and "both" for decisions made for both.

{rules}

Only extract from messages after the "--- new ---" line; earlier lines are context.

- facts: durable preferences, places they like or dislike, constraints (allergies, diet). One \
plain-English sentence each, keeping local terms ("prefers to tapao (takeaway) on weekdays"). \
Skip one-off moods and small talk. confidence 0-1.
- episodes: each time they decided (or failed to decide) between options: what kind of decision \
(category_phrase in their own words, e.g. "dinner", "makan where", "which movie"), what was \
considered, and the outcome. choice is the chosen option when outcome is "chosen", else "". ts \
is the ISO time of the decision.
- options: specific named options mentioned for a kind of decision (a restaurant, a show), with \
sentiment -1..1.
- ⟦place: Name · … · place_id=N⟧ after a link marks a Google Maps place someone shared: use \
Name exactly as the option name or choice. "⟦location shared⟧" is an unnamed location or a \
home: never extract anything about it.
Return empty lists when there's nothing."""


def windows(rows: Sequence[StoredMessage]) -> list[list[StoredMessage]]:
    """Split on gaps > 2 h or ~20k characters (§15.3 windowing)."""
    out: list[list[StoredMessage]] = []
    current: list[StoredMessage] = []
    chars = 0
    for row in rows:
        gap = current and from_sql(row.created_at) - from_sql(current[-1].created_at) > WINDOW_GAP
        if current and (gap or chars + len(row.text) > WINDOW_MAX_CHARS):
            out.append(current)
            current, chars = [], 0
        current.append(row)
        chars += len(row.text)
    if current:
        out.append(current)
    return out


def _parse_ts(value: str, fallback: datetime, tz: ZoneInfo) -> datetime:
    """The model's ``ts`` for an episode. The transcript shows household-local times without an
    offset, so a naive value is local time. Anything unparseable or later than the window's last
    message (a hallucinated date) falls back to that message's time."""
    try:
        ts = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return fallback
    ts = ts if ts.tzinfo else ts.replace(tzinfo=tz)
    return fallback if ts > fallback else ts


class Harvester:
    def __init__(
        self,
        *,
        db: Database,
        settings: SettingsStore,
        llm: LLMClient,
        memory: MemoryService,
        decisions: DecisionService,
        topics: TopicService,
        users: Sequence[UserRecord],
        tz: ZoneInfo,
        group_id: Callable[[], int | None],
        health: HealthState | None = None,
        places: PlaceService | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._places = places
        self._db = db
        self._settings = settings
        self._llm = llm
        self._memory = memory
        self._decisions = decisions
        self._topics = topics
        self._users = list(users)
        self._users_by_id = {u.id: u for u in users}
        self._slugs = [u.slug for u in users]
        self._tz = tz
        self._group_id = group_id
        self._health = health
        self._clock = clock
        self._last_tick: datetime | None = None
        # The scheduler never overlaps itself, but the dashboard's "harvest now" can land mid-run;
        # two runs would read the same cursor and apply everything twice.
        self._lock = asyncio.Lock()
        self._bad_output: dict[tuple[int, int], int] = {}  # (thread, first msg id) → bad outputs

    # --- stage 1: tick (code only) -----------------------------------------------------------

    async def backlog(self, group: int, answer: int) -> list[Backlog]:
        rows = await self._db.read(lambda c: c.execute(BACKLOG_SQL, (group, answer)).fetchall())
        return [Backlog(int(r[0]), int(r[1]), from_sql(r[2]), int(r[3])) for r in rows]

    async def tick(self, *, force: bool = False) -> list[RunResult]:
        """Called every minute by the scheduler; does real work only every
        ``harvest.interval_min`` (hot-reloaded), or when ``force``d from the dashboard."""
        async with self._lock:
            return await self._tick(force=force)

    async def _tick(self, *, force: bool) -> list[RunResult]:
        s = await self._settings.load()
        now = self._clock()
        if not s.harvest_enabled and not force:
            return []
        if (
            not force
            and self._last_tick is not None
            and now - self._last_tick < timedelta(minutes=s.harvest_interval_min)
        ):
            return []
        self._last_tick = now
        if self._health is not None:
            self._health.last_harvest_tick_at = now
        group, answer = self._group_id(), s.telegram_answer_topic_id
        if group is None or answer is None:
            return []  # no answer topic yet: every topic is answered live, nothing to harvest

        # Ignored topics are never sent anywhere (§10.4), including rows stored before the topic
        # was ignored.
        ignored = set(s.telegram_ignored_topic_ids)
        backlog = [b for b in await self.backlog(group, answer) if b.thread_id not in ignored]
        await self._db.write(
            lambda c: c.execute(
                "UPDATE topic_harvest SET last_tick_at = ? WHERE chat_id = ?", (to_sql(now), group)
            )
        )
        due = [
            b
            for b in backlog
            if b.new_msgs >= s.harvest_min_new_messages
            or now - b.oldest_new > timedelta(hours=s.harvest_max_age_hours)
        ]
        log.info(
            "harvest tick",
            extra={"topics_with_new": len(backlog), "due": [b.thread_id for b in due]},
        )
        if not due:
            return []
        if await self._over_warn_budget(s):
            log.warning("harvest paused: budget above warning level")
            return [RunResult(b.thread_id, "budget") for b in due]
        return [await self._harvest(group, b, s) for b in due]

    async def _over_warn_budget(self, s: RuntimeSettings) -> bool:
        """§10.4: the harvester pauses at 80% of either cap, before anything user-facing."""
        st = await budget_status(self._db, s, self._tz)
        return (
            st.daily_spent >= s.budget_warn_ratio * st.daily_cap
            or st.monthly_spent >= s.budget_warn_ratio * st.monthly_cap
        )

    # --- stage 2: LLM harvest ----------------------------------------------------------------

    async def _rows(
        self, group: int, b: Backlog, s: RuntimeSettings
    ) -> tuple[list[StoredMessage], list[StoredMessage], int]:
        from app.db.repos import messages as messages_repo

        def _q(c: sqlite3.Connection) -> tuple[list[StoredMessage], list[StoredMessage], int]:
            thread_sql = "thread_id IS NULL" if b.thread_id == NO_TOPIC else "thread_id = ?"
            targs: tuple[int, ...] = () if b.thread_id == NO_TOPIC else (b.thread_id,)
            cur = c.execute(
                "SELECT last_msg_id FROM topic_harvest WHERE chat_id = ? AND thread_id = ?",
                (group, b.thread_id),
            ).fetchone()
            cursor = int(cur[0]) if cur else 0
            new = c.execute(
                f"SELECT * FROM messages WHERE chat_id = ? AND {thread_sql} AND role = 'user' "
                f"AND id > ? AND id <= ? ORDER BY id",
                (group, *targs, cursor, b.newest_id),
            ).fetchall()
            ctx = c.execute(
                f"SELECT * FROM messages WHERE chat_id = ? AND {thread_sql} "
                f"AND role IN ('user', 'assistant') AND id <= ? ORDER BY id DESC LIMIT ?",
                (group, *targs, cursor, s.harvest_context_messages),
            ).fetchall()
            return (
                [messages_repo.from_row(r) for r in new],
                [messages_repo.from_row(r) for r in reversed(ctx)],
                cursor,
            )

        return await self._db.read(_q)

    def _line(self, row: StoredMessage) -> str:
        user = self._users_by_id.get(row.user_id) if row.user_id is not None else None
        who = user.slug if user else ("tykee" if row.role == "assistant" else "someone")
        when = from_sql(row.created_at).astimezone(self._tz).strftime("%Y-%m-%d %H:%M")
        return f"[{when}] {who}: {row.text.strip()}"

    async def _harvest(self, group: int, b: Backlog, s: RuntimeSettings) -> RunResult:
        new, context, _ = await self._rows(group, b, s)
        result = RunResult(b.thread_id, "done", messages=len(new))
        if not new:
            await self._advance(group, b.thread_id, b.newest_id)
            return result
        topic_name = await self._topics.name_of(
            group, None if b.thread_id == NO_TOPIC else b.thread_id
        )
        if b.thread_id == NO_TOPIC:
            topic_name = "earlier chat"
        system = SYSTEM_PROMPT.format(
            users=", ".join(f"{u.slug} ({u.display_name})" for u in self._users),
            rules=SAFE_TOPIC_RULES,
        )
        prior = [self._line(r) for r in context]
        for i, win in enumerate(windows(new)):
            if i > 0 and await self._over_warn_budget(s):
                result.status = "budget"
                return await self._record(group, b, new, result)
            transcript = "\n".join([*prior, "--- new ---", *(self._line(r) for r in win)])
            try:
                resp = await self._llm.complete(
                    LLMRequest(
                        purpose="harvest",
                        model_role="harvest",
                        system=[
                            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
                        ],
                        messages=[
                            {"role": "user", "content": f"Topic: {topic_name}\n\n{transcript}"}
                        ],
                        max_tokens=HARVEST_MAX_TOKENS,
                        json_schema=EXTRACTION_SCHEMA,
                        chat_id=group,
                        timeout_s=120.0,
                    )
                )
                extraction = Extraction.model_validate(json.loads(resp.text))
            except BudgetExceeded:
                result.status = "budget"
                return await self._record(group, b, new, result)
            except LLMError as e:
                # API down/timeouts: retried next tick, never skipped.
                return await self._failed(group, b, new, result, type(e).__name__)
            except (json.JSONDecodeError, ValidationError) as e:
                key = (b.thread_id, win[0].id)
                self._bad_output[key] = self._bad_output.get(key, 0) + 1
                if self._bad_output[key] < MAX_BAD_OUTPUT:
                    return await self._failed(group, b, new, result, type(e).__name__)
                # The model keeps choking on this window: skip it rather than pay forever.
                del self._bad_output[key]
                log.warning(
                    "harvest window skipped",
                    extra={"thread_id": b.thread_id, "from": win[0].id, "to": win[-1].id},
                )
                result.status = "skipped"
                result.errors.append(type(e).__name__)
                await self._advance(group, b.thread_id, win[-1].id)
                prior = [self._line(r) for r in win[-s.harvest_context_messages :]]
                continue
            self._bad_output.pop((b.thread_id, win[0].id), None)
            last = win[-1]
            source = f"topic:{topic_name}/msg:{last.tg_message_id or last.id}"
            marked = [a for r in win for a in place_links.annotations_in(r.text)]
            await self._apply(
                extraction, result, source, topic_name, from_sql(last.created_at), marked
            )
            # Advance per window: a later failure must not re-propose what already landed.
            await self._advance(group, b.thread_id, last.id)
            prior = [self._line(r) for r in win[-s.harvest_context_messages :]]
        await self._advance(group, b.thread_id, b.newest_id)
        return await self._record(group, b, new, result)

    async def _failed(
        self, group: int, b: Backlog, new: Sequence[StoredMessage], result: RunResult, error: str
    ) -> RunResult:
        """Keep what earlier windows of this run already applied in the counts."""
        log.warning("harvest failed", extra={"thread_id": b.thread_id, "error": error})
        result.status = "error"
        result.errors.append(error)
        return await self._record(group, b, new, result)

    async def _apply(
        self,
        ex: Extraction,
        result: RunResult,
        source: str,
        topic: str,
        fallback_ts: datetime,
        marked: Sequence[place_links.Annotated] = (),
    ) -> None:
        result.skipped_out_of_scope += ex.skipped_out_of_scope
        for fact in ex.facts:
            if fact.confidence < MIN_FACT_CONFIDENCE:
                continue
            owner = fact.owner if fact.owner in (*self._slugs, "shared") else None
            if owner is None:
                continue  # third parties are never stored (§15.4)
            reason = f'Said in {topic}: "{fact.quote}"' if fact.quote else f"Said in {topic}"
            try:
                await self._memory.propose(
                    owner=owner,
                    content=fact.statement,
                    reason=reason[:300],
                    topic=_FACT_FILES[fact.type],
                    source=source,
                )
                result.facts += 1
            except MemoryPolicyError as e:
                result.errors.append(str(e))

        suggested_categories: set[str] = set()
        for ep in ex.episodes:
            if ep.outcome != "chosen" or not ep.choice.strip():
                continue
            category = None
            for phrase in [ep.category_phrase, *ep.phrases_seen]:
                category = await self._decisions.lookup(phrase)
                if category is not None:
                    break
            if category is None:
                key = ep.category_phrase.strip().casefold()
                if key and key not in suggested_categories:
                    suggested_categories.add(key)
                    await self._memory.suggest(
                        kind="category",
                        content=f"New kind of decision: {ep.category_phrase} "
                        f"(e.g. chose {ep.choice})",
                        reason=f"Decided in {topic}: {ep.summary}"[:300],
                        source=source,
                        payload={
                            "phrase": ep.category_phrase,
                            "aliases": ep.phrases_seen,
                            "description": ep.summary[:200],
                        },
                    )
                    result.suggestions += 1
                continue
            for_users = ep.for_users if ep.for_users in (*self._slugs, "both") else "both"
            choice = ep.choice.strip()
            place = None
            if (pid := place_links.match_place(choice, marked)) is not None and self._places:
                place = await self._places.get(pid)
            await self._decisions.record_observed(
                category,
                choice=place.name if place else choice,
                for_users=for_users,
                at=_parse_ts(ep.ts, fallback_ts, self._tz),
                chat_id=self._group_id(),
                place_id=place.id if place else None,
            )
            if place is not None and self._places is not None:
                await self._places.visit(place.id)
            result.decisions += 1

        for opt in ex.options:
            category = await self._decisions.lookup(opt.category_phrase)
            if category is None or not opt.name.strip() or opt.sentiment < 0:
                continue
            known = {o.name.casefold() for o in await self._decisions.list_options(category)}
            if opt.name.strip().casefold() in known:
                continue
            payload: dict[str, object] = {
                "category_id": category.id,
                "name": opt.name.strip(),
                "tags": opt.tags,
            }
            if (pid := place_links.match_place(opt.name, marked)) is not None:
                payload["place_id"] = pid
            await self._memory.suggest(
                kind="option",
                content=f"New {category.display_name.lower()} option: {opt.name}",
                reason=f"Mentioned in {topic}",
                source=source,
                payload=payload,
            )
            result.suggestions += 1

    async def _advance(self, group: int, thread_id: int, last_msg_id: int) -> None:
        now = to_sql(self._clock())
        await self._db.write(
            lambda c: c.execute(
                "INSERT INTO topic_harvest(chat_id, thread_id, last_msg_id, last_run_at, "
                "last_tick_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(chat_id, thread_id) DO UPDATE "
                "SET last_msg_id = excluded.last_msg_id, last_run_at = excluded.last_run_at, "
                "last_tick_at = excluded.last_tick_at",
                (group, thread_id, last_msg_id, now, now),
            )
        )

    async def _record(
        self, group: int, b: Backlog, new: Sequence[StoredMessage], r: RunResult
    ) -> RunResult:
        first = new[0].id if new else b.newest_id
        await self._db.write(
            lambda c: c.execute(
                "INSERT INTO harvest_runs(chat_id, thread_id, from_msg_id, to_msg_id, messages, "
                "facts, decisions, suggestions, skipped_out_of_scope, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    group,
                    b.thread_id,
                    first,
                    b.newest_id,
                    len(new),
                    r.facts,
                    r.decisions,
                    r.suggestions,
                    r.skipped_out_of_scope,
                    r.status,
                    to_sql(self._clock()),
                ),
            )
        )
        log.info(
            "harvested topic",
            extra={
                "thread_id": b.thread_id,
                "status": r.status,
                "messages": len(new),
                "facts": r.facts,
                "decisions": r.decisions,
                "suggestions": r.suggestions,
            },
        )
        return r
