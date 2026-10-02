"""Rolling chat summaries (§7.2): once at least ``summary.batch`` messages have fallen out of the
replay window, fold them into the chat's summary with one Haiku-tier call. Runs in the
background after replies and judge calls; at most one run per chat at a time."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from zoneinfo import ZoneInfo

from app.db.database import Database
from app.db.repos import messages as messages_repo
from app.db.repos import summaries as summaries_repo
from app.db.repos.users import UserRecord
from app.llm.client import LLMClient, LLMError, LLMRequest
from app.orchestrator.history import format_transcript
from app.settings import SettingsStore

log = logging.getLogger(__name__)

SUMMARY_SYSTEM = (
    "You maintain a running summary of a private chat between a couple and their decision bot, "
    "Tykee. Merge the previous summary with the new messages into one updated summary of at most "
    "150 words. Keep what matters for future decisions: choices made, preferences, plans, open "
    "questions. Drop small talk. Plain text, no headings."
)
SUMMARY_MAX_TOKENS = 400


class Summarizer:
    def __init__(
        self,
        *,
        db: Database,
        settings: SettingsStore,
        llm: LLMClient,
        users: Sequence[UserRecord],
        tz: ZoneInfo,
        history_thread: Callable[[int], Awaitable[int | None]] | None = None,
    ) -> None:
        self._history_thread = history_thread
        self._db = db
        self._settings = settings
        self._llm = llm
        self._users_by_id = {u.id: u for u in users}
        self._tz = tz
        self._running: dict[int, asyncio.Task[None]] = {}

    def schedule(self, chat_id: int) -> None:
        task = self._running.get(chat_id)
        if task is not None and not task.done():
            return
        self._running[chat_id] = asyncio.create_task(self.run(chat_id))

    async def run(self, chat_id: int) -> None:
        s = await self._settings.load()
        current = await self._db.read(lambda c: summaries_repo.get(c, chat_id))
        upto = current.upto_msg_id if current else 0
        thread = await self._history_thread(chat_id) if self._history_thread else None
        rows = await self._db.read(
            lambda c: messages_repo.after(c, chat_id, upto, only_thread=thread)
        )
        overflow = rows[: max(len(rows) - s.history_max_turns, 0)]
        if len(overflow) < s.summary_batch:
            return
        transcript = format_transcript(overflow, self._users_by_id, self._tz)
        previous = current.text if current else "(none yet)"
        try:
            resp = await self._llm.complete(
                LLMRequest(
                    purpose="summary",
                    model_role="default",
                    system=[{"type": "text", "text": SUMMARY_SYSTEM}],
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                f"Previous summary:\n{previous}\n\nNew messages:\n{transcript}"
                            ),
                        }
                    ],
                    max_tokens=SUMMARY_MAX_TOKENS,
                    chat_id=chat_id,
                    timeout_s=60.0,
                )
            )
        except LLMError:
            log.warning("summary skipped: llm unavailable", extra={"chat_id": chat_id})
            return
        text = resp.text.strip()
        if not text:
            return
        last_id = overflow[-1].id
        await self._db.write(lambda c: summaries_repo.upsert(c, chat_id, text, last_id))
        log.info("chat summarised", extra={"chat_id": chat_id, "messages": len(overflow)})

    async def close(self) -> None:
        tasks = [t for t in self._running.values() if not t.done()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
