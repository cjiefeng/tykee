"""Turn handling (§9): prompt → Claude tool loop → reply, with templated fallback (§8.5)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from anthropic.types import (
    ContentBlock,
    ContentBlockParam,
    MessageParam,
    ToolResultBlockParam,
)

from app.db.database import Database
from app.db.repos import messages as messages_repo
from app.db.repos.users import UserRecord
from app.decisions.engine import PickRequest
from app.decisions.service import DecisionService
from app.llm.client import BudgetExceeded, LLMClient, LLMError, LLMRequest, LLMResponse
from app.orchestrator.history import build_messages
from app.orchestrator.prompt import build_system, dynamic_context
from app.orchestrator.tools import ToolRouter, TurnContext
from app.settings import SettingsStore
from app.timeutil import utcnow

log = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 6
FALLBACK_OFFLINE = "My brain's offline right now 🤕"
FALLBACK_BUDGET = "I've hit my spending cap for now 💸"
FALLBACK_EMPTY = "🤔"


@dataclass(frozen=True)
class ChatContext:
    chat_id: int
    is_group: bool


@dataclass(frozen=True)
class Reply:
    text: str
    from_llm: bool  # False for fallback text, which is not stored in history
    picks: list[tuple[int, str]] = field(default_factory=list)  # (decision_id, name) → buttons


def assistant_blocks(content: Sequence[ContentBlock]) -> list[ContentBlockParam]:
    """Echo Claude's turn back as request params: text and tool_use only, built field by field
    so response-only attributes never leak into the next request."""
    out: list[ContentBlockParam] = []
    for b in content:
        if b.type == "text" and b.text:
            out.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
    return out


def default_for_users(chat: ChatContext, actor: UserRecord) -> str:
    return "both" if chat.is_group else actor.slug


def bold_list(names: Sequence[str]) -> str:
    return ", ".join(f"**{n}**" for n in names)


class Orchestrator:
    def __init__(
        self,
        *,
        db: Database,
        settings: SettingsStore,
        llm: LLMClient,
        decisions: DecisionService,
        users: Sequence[UserRecord],
        tz: ZoneInfo,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._settings = settings
        self._llm = llm
        self._decisions = decisions
        self._tools = ToolRouter(decisions)
        self._users = list(users)
        self._users_by_id = {u.id: u for u in users}
        self._tz = tz
        self._clock = clock

    async def respond(self, chat: ChatContext, actor: UserRecord, text: str) -> Reply:
        s = await self._settings.load()
        rows = await self._db.read(
            lambda conn: messages_repo.recent(conn, chat.chat_id, s.history_max_turns)
        )
        messages: list[MessageParam] = build_messages(
            rows, self._users_by_id, is_group=chat.is_group
        )
        if not messages:
            return Reply(FALLBACK_EMPTY, from_llm=False)
        ctx = TurnContext(
            chat_id=chat.chat_id,
            actor=actor,
            default_for_users=default_for_users(chat, actor),
            tz=self._tz,
        )
        system = build_system(
            s.persona_system_prompt,
            dynamic_context(
                now=self._clock(),
                actor=actor,
                users=self._users,
                is_group=chat.is_group,
                default_for_users=ctx.default_for_users,
            ),
        )
        try:
            resp = await self._loop(system, messages, ctx)
        except BudgetExceeded as e:
            log.warning("budget exhausted", extra={"period": e.period})
            return await self._fallback(FALLBACK_BUDGET, chat, actor, text, ctx)
        except LLMError:
            return await self._fallback(FALLBACK_OFFLINE, chat, actor, text, ctx)

        if resp.message.stop_reason == "refusal":
            return Reply("I'd rather not help with that one.", from_llm=False, picks=ctx.last_picks)
        out = resp.text.strip()
        if not out:
            if ctx.last_picks:
                out = f"🎲 {bold_list([n for _, n in ctx.last_picks])}"
            else:
                return Reply(FALLBACK_EMPTY, from_llm=False)
        return Reply(out, from_llm=True, picks=ctx.last_picks)

    async def _loop(
        self, system: Any, messages: list[MessageParam], ctx: TurnContext
    ) -> LLMResponse:
        tools = self._tools.definitions()
        for i in range(MAX_TOOL_ITERATIONS + 1):
            last = i == MAX_TOOL_ITERATIONS
            resp = await self._llm.complete(
                LLMRequest(
                    purpose="chat",
                    model_role="default",
                    system=system,
                    messages=messages,
                    tools=tools,
                    tool_choice={"type": "none"} if last else None,
                    user_id=ctx.actor.id,
                    chat_id=ctx.chat_id,
                )
            )
            if resp.message.stop_reason != "tool_use" or last:
                return resp
            messages = [
                *messages,
                {
                    "role": "assistant",
                    "content": assistant_blocks(resp.message.content),
                },
                {"role": "user", "content": await self._run_tools(resp, ctx)},
            ]
        raise AssertionError("unreachable")

    async def _run_tools(self, resp: LLMResponse, ctx: TurnContext) -> list[ContentBlockParam]:
        results: list[ContentBlockParam] = []
        for block in resp.message.content:
            if block.type != "tool_use":
                continue
            raw = block.input if isinstance(block.input, dict) else {}
            outcome = await self._tools.execute(block.name, raw, ctx)
            log.info("tool call", extra={"tool": block.name, "error": outcome.is_error})
            result: ToolResultBlockParam = {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": outcome.content,
            }
            if outcome.is_error:
                result["is_error"] = True
            results.append(result)
            await self._store_tool_call(ctx.chat_id, block.name, raw, outcome.content)
        return results

    async def _store_tool_call(
        self, chat_id: int, name: str, raw: dict[str, Any], result: str
    ) -> None:
        content = json.dumps(
            [
                {"type": "tool_use", "name": name, "input": raw},
                {"type": "tool_result", "content": result},
            ],
            ensure_ascii=False,
        )
        await self._db.write(
            lambda conn: messages_repo.insert(
                conn,
                chat_id=chat_id,
                tg_message_id=None,
                user_id=None,
                role="tool",
                kind="other",
                content=content,
            )
        )

    async def _fallback(
        self, prefix: str, chat: ChatContext, actor: UserRecord, text: str, ctx: TurnContext
    ) -> Reply:
        """§8.5: pick from an alias match in code; keep a pick Claude already made this turn."""
        if ctx.last_picks:
            names = bold_list([n for _, n in ctx.last_picks])
            return Reply(f"{prefix} but I did pick one: {names} 🎲", False, ctx.last_picks)
        category = await self._decisions.match_in_text(text)
        if category is None:
            return Reply(f"{prefix} Try /pick <category>.", from_llm=False)
        req = PickRequest(category_id=category.id, for_users=ctx.default_for_users)
        result = await self._decisions.pick(category, req, asked_by=actor.id, chat_id=chat.chat_id)
        if not result.picks:
            return Reply(
                f"{prefix} and I have no saved options for {category.display_name} yet.",
                from_llm=False,
            )
        picks = [(p.decision_id, p.name) for p in result.picks]
        names = bold_list([n for _, n in picks])
        return Reply(
            f"{prefix} Here's a random {category.display_name.lower()} pick: {names} 🎲",
            from_llm=False,
            picks=picks,
        )
