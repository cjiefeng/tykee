"""Ambient participation (§10.2): listen to group chatter, debounce bursts, apply stage-1 rules,
ask the judge, and speak only when it's clearly welcome. Also handles mute and feedback."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.ambient import state as state_repo
from app.ambient.debounce import Debouncer
from app.ambient.judge import Judge, JudgeError
from app.ambient.phrases import matches_any
from app.ambient.rules import stage1
from app.brain.memory import MemoryService
from app.db.database import Database
from app.db.repos import messages as messages_repo
from app.db.repos import summaries as summaries_repo
from app.db.repos.users import UserRecord
from app.decisions.service import DecisionService
from app.llm.client import BudgetExceeded, LLMError
from app.orchestrator.history import format_transcript
from app.orchestrator.summary import Summarizer
from app.settings import SettingsStore
from app.timeutil import utcnow

log = logging.getLogger(__name__)

NEGATIVE_EMOJI = "👎"
POSITIVE_EMOJI = "👍"

# chat_id, actor, burst text, judge's reason → first sent Telegram message id, or None if the
# responder decided not to send anything.
Responder = Callable[[int, UserRecord, str, str], Awaitable[int | None]]


@dataclass
class _Burst:
    from_msg_id: int
    to_msg_id: int
    last_actor: UserRecord
    kinds: set[str] = field(default_factory=set)
    texts: list[str] = field(default_factory=list)


class AmbientService:
    def __init__(
        self,
        *,
        db: Database,
        settings: SettingsStore,
        judge: Judge,
        decisions: DecisionService,
        summarizer: Summarizer,
        users: Sequence[UserRecord],
        tz: ZoneInfo,
        memory: MemoryService | None = None,
        history_thread: Callable[[int], Awaitable[int | None]] | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._history_thread = history_thread
        self._memory = memory
        self._db = db
        self._settings = settings
        self._judge = judge
        self._decisions = decisions
        self._summarizer = summarizer
        self._users_by_id = {u.id: u for u in users}
        self._tz = tz
        self._clock = clock
        self._pending: dict[int, _Burst] = {}
        self._debouncer = Debouncer(self.fire)
        self.responder: Responder | None = None

    def _today(self) -> str:
        return self._clock().astimezone(self._tz).date().isoformat()

    # --- listening ------------------------------------------------------------------------

    async def on_chatter(
        self, chat_id: int, row_id: int, actor: UserRecord, kind: str, text: str
    ) -> None:
        """A group message that didn't address the bot: extend the burst and restart the timer."""
        s = await self._settings.load()
        if not s.ambient_enabled:
            return
        burst = self._pending.get(chat_id)
        if burst is None:
            burst = self._pending[chat_id] = _Burst(row_id, row_id, actor)
        burst.to_msg_id = row_id
        burst.last_actor = actor
        burst.kinds.add(kind)
        if text:
            burst.texts.append(text)
        self._debouncer.touch(chat_id, s.ambient_debounce_s)

    def cancel(self, chat_id: int) -> None:
        """The bot was addressed directly: it's engaged now, so drop the pending burst."""
        self._debouncer.cancel(chat_id)
        self._pending.pop(chat_id, None)

    async def fire(self, chat_id: int) -> None:
        burst = self._pending.pop(chat_id, None)
        if burst is None:
            return
        s = await self._settings.load()
        now = self._clock()
        today = self._today()
        st = await self._db.read(lambda c: state_repo.load(c, chat_id, today))

        async def _log(action: str, **kw: object) -> int:
            return await self._db.write(
                lambda c: state_repo.log(
                    c,
                    chat_id=chat_id,
                    from_msg_id=burst.from_msg_id,
                    to_msg_id=burst.to_msg_id,
                    action=action,
                    now=now,
                    **kw,  # type: ignore[arg-type]
                )
            )

        rule = stage1(
            st,
            burst.kinds,
            now=now,
            enabled=s.ambient_enabled,
            cooldown_min=s.ambient_cooldown_min,
            max_per_day=s.ambient_max_per_day,
        )
        if rule is not None:
            await _log("skipped_rule", rule=rule)
            log.info("ambient skipped", extra={"chat_id": chat_id, "rule": rule})
            return

        thread = await self._history_thread(chat_id) if self._history_thread else None
        rows = await self._db.read(
            lambda c: messages_repo.recent(
                c, chat_id, s.ambient_window_messages, only_thread=thread
            )
        )
        summary = await self._db.read(lambda c: summaries_repo.get(c, chat_id))
        today_decisions = await self._decisions.today(chat_id, self._tz)
        context_lines = [f"Now: {now.astimezone(self._tz):%a %H:%M}"]
        if summary:
            context_lines.append(f"Earlier in this chat: {summary.text}")
        pinned = await self._memory.pinned_block() if self._memory is not None else None
        if pinned:
            context_lines.append(pinned)
        if today_decisions:
            context_lines.append(
                "Decisions today: "
                + "; ".join(
                    f"{cat}: {choice} ({status})" for cat, choice, status in today_decisions
                )
            )
        transcript = format_transcript(
            rows, self._users_by_id, self._tz, marker_before_id=burst.from_msg_id
        )
        try:
            verdict = await self._judge.judge(
                prompt=s.ambient_judge_prompt,
                context="\n".join(context_lines),
                transcript=transcript,
                chat_id=chat_id,
            )
        except BudgetExceeded:
            await _log("skipped_rule", rule="budget")
            return
        except (LLMError, JudgeError) as e:
            await _log("skipped_rule", rule="judge_error", reason=type(e).__name__)
            return
        finally:
            self._summarizer.schedule(chat_id)

        speak = verdict.action == "respond" and verdict.confidence >= s.ambient_threshold
        log.info(
            "ambient judged",
            extra={"chat_id": chat_id, "speak": speak, "confidence": verdict.confidence},
        )
        if not speak or self.responder is None:
            await _log("silent", reason=verdict.reason, confidence=verdict.confidence)
            return

        text = "\n".join(burst.texts)
        sent_id = await self.responder(chat_id, burst.last_actor, text, verdict.reason)
        if sent_id is None:
            await _log(
                "silent", rule="no_reply", reason=verdict.reason, confidence=verdict.confidence
            )
            return
        await self._db.write(lambda c: state_repo.record_unprompted(c, chat_id, now, today))
        await _log(
            "respond",
            reason=verdict.reason,
            confidence=verdict.confidence,
            reply_tg_message_id=sent_id,
        )

    # --- mute -------------------------------------------------------------------------------

    async def is_mute_request(self, text: str) -> bool:
        s = await self._settings.load()
        return matches_any(text, s.ambient_mute_phrases)

    async def mute(self, chat_id: int, duration: timedelta | None = None) -> datetime:
        s = await self._settings.load()
        until = self._clock() + (duration or timedelta(minutes=s.ambient_default_mute_min))
        await self._db.write(lambda c: state_repo.set_mute(c, chat_id, until))
        self.cancel(chat_id)
        log.info("ambient muted", extra={"chat_id": chat_id, "until": until.isoformat()})
        return until

    async def unmute(self, chat_id: int) -> None:
        await self._db.write(lambda c: state_repo.set_mute(c, chat_id, None))

    # --- feedback ---------------------------------------------------------------------------

    async def _negative(self, chat_id: int, log_id: int) -> None:
        today = self._today()

        def _apply(c: sqlite3.Connection) -> bool:
            changed = state_repo.set_feedback(c, log_id, "negative")
            if changed:
                state_repo.double_cooldown(c, chat_id, today)
            return changed

        if await self._db.write(_apply):
            log.info("ambient negative feedback", extra={"chat_id": chat_id, "log_id": log_id})

    async def on_reaction(self, chat_id: int, tg_message_id: int, emojis: set[str]) -> None:
        log_id = await self._db.read(
            lambda c: state_repo.unprompted_by_message(c, chat_id, tg_message_id)
        )
        if log_id is None:
            return
        if NEGATIVE_EMOJI in emojis:
            await self._negative(chat_id, log_id)
        elif POSITIVE_EMOJI in emojis:
            await self._db.write(lambda c: state_repo.set_feedback(c, log_id, "positive"))

    async def check_negative_text(self, chat_id: int, text: str) -> bool:
        """'not now', 'didn't ask'… shortly after an unprompted reply counts as a false positive."""
        s = await self._settings.load()
        if not matches_any(text, s.ambient_negative_phrases):
            return False
        since = self._clock() - timedelta(minutes=s.ambient_negative_window_min)
        log_id = await self._db.read(
            lambda c: state_repo.latest_unprompted_since(c, chat_id, since)
        )
        if log_id is None:
            return False
        await self._negative(chat_id, log_id)
        return True

    async def close(self) -> None:
        await self._debouncer.close()
