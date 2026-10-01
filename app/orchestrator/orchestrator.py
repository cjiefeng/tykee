"""Turn handling (§9). M1: one Claude call with persona + history, no tools yet."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from app.db.database import Database
from app.db.repos import messages as messages_repo
from app.db.repos.users import UserRecord
from app.llm.client import BudgetExceeded, LLMClient, LLMError, LLMRequest
from app.orchestrator.history import build_messages
from app.orchestrator.prompt import build_system, dynamic_context
from app.settings import SettingsStore
from app.timeutil import utcnow

log = logging.getLogger(__name__)

FALLBACK_OFFLINE = "My brain's offline right now 🤕 Try again in a bit."
FALLBACK_BUDGET = "I've hit my spending cap for now 💸 Try again later."
FALLBACK_EMPTY = "🤔"


@dataclass(frozen=True)
class ChatContext:
    chat_id: int
    is_group: bool


@dataclass(frozen=True)
class Reply:
    text: str
    from_llm: bool  # False for fallback text, which is not stored in history


class Orchestrator:
    def __init__(
        self,
        *,
        db: Database,
        settings: SettingsStore,
        llm: LLMClient,
        users: Sequence[UserRecord],
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._settings = settings
        self._llm = llm
        self._users = list(users)
        self._users_by_id = {u.id: u for u in users}
        self._clock = clock

    async def respond(self, chat: ChatContext, actor: UserRecord) -> Reply:
        s = await self._settings.load()
        rows = await self._db.read(
            lambda conn: messages_repo.recent(conn, chat.chat_id, s.history_max_turns)
        )
        messages = build_messages(rows, self._users_by_id, is_group=chat.is_group)
        if not messages:
            return Reply(FALLBACK_EMPTY, from_llm=False)
        system = build_system(
            s.persona_system_prompt,
            dynamic_context(
                now=self._clock(), actor=actor, users=self._users, is_group=chat.is_group
            ),
        )
        req = LLMRequest(
            purpose="chat",
            model_role="default",
            system=system,
            messages=messages,
            user_id=actor.id,
            chat_id=chat.chat_id,
        )
        try:
            resp = await self._llm.complete(req)
        except BudgetExceeded as e:
            log.warning("budget exhausted", extra={"period": e.period})
            return Reply(FALLBACK_BUDGET, from_llm=False)
        except LLMError:
            return Reply(FALLBACK_OFFLINE, from_llm=False)
        if resp.message.stop_reason == "refusal":
            return Reply("I'd rather not help with that one.", from_llm=False)
        text = resp.text.strip()
        return Reply(text, from_llm=True) if text else Reply(FALLBACK_EMPTY, from_llm=False)
