"""Turn handling (§9): prompt → Claude tool loop → reply, with templated fallback (§8.5)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from anthropic.types import (
    ContentBlock,
    ContentBlockParam,
    MessageParam,
    ToolResultBlockParam,
    ToolUnionParam,
)

from app.brain.memory import MemoryService
from app.db.database import Database
from app.db.repos import messages as messages_repo
from app.db.repos import summaries as summaries_repo
from app.db.repos.users import UserRecord
from app.decisions.engine import PickRequest
from app.decisions.service import DecisionService
from app.health import HealthState
from app.llm.client import (
    INTERACTIVE_TIMEOUT_S,
    BudgetExceeded,
    LLMBadRequest,
    LLMClient,
    LLMError,
    LLMRequest,
    LLMResponse,
    budget_status,
)
from app.orchestrator import escalation
from app.orchestrator.escalation import TIER_RANK, Tier
from app.orchestrator.history import build_messages
from app.orchestrator.prompt import build_system, dynamic_context
from app.orchestrator.summary import Summarizer
from app.orchestrator.tools import ToolOutcome, ToolRouter, TurnContext
from app.orchestrator.web import (
    HANDOFF_TOOL,
    handoff_tool,
    server_calls,
    sources,
    used_web,
    web_status,
    web_tools,
    with_sources,
)
from app.places.recommend import RecommendService
from app.places.service import PlaceService
from app.settings import RuntimeSettings, SettingsStore
from app.timeutil import utcnow

log = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 6
DEEP_TIMEOUT_S = 90.0  # Sonnet/Opus-tier replies write more and take longer (§7.1)
FALLBACK_OFFLINE = "My brain's offline right now 🤕"
FALLBACK_BUDGET = "I've hit my spending cap for now 💸"
FALLBACK_EMPTY = "🤔"
# Server-side web tool blocks (§7.5) go back to the API exactly as received: search results carry
# encrypted_content the API needs to restore them on the next request.
SERVER_BLOCKS = frozenset({"server_tool_use", "web_search_tool_result", "web_fetch_tool_result"})


@dataclass(frozen=True)
class ChatContext:
    chat_id: int
    is_group: bool
    thread: int | None = None  # history filter: the answer topic in a forum group (§10.4)


@dataclass(frozen=True)
class Reply:
    text: str
    from_llm: bool  # False for fallback text, which is not stored in history
    picks: list[tuple[int, str]] = field(default_factory=list)  # (decision_id, name) → buttons
    budget_exhausted: bool = False  # §14.4: the adapter DMs the admin once a day
    recorded: list[tuple[int, int | None]] = field(default_factory=list)  # → reaction (§10.5)
    recommendation: bool = False  # picks are find_places picks: [✅ 1] [✅ 2] … [🎲 more]
    tier: Tier = "default"  # §7.1: the model tier that answered


def assistant_blocks(content: Sequence[ContentBlock]) -> list[ContentBlockParam]:
    """Echo Claude's turn back as request params: text and tool_use built field by field so
    response-only attributes never leak into the next request; server web tool blocks verbatim."""
    out: list[ContentBlockParam] = []
    for b in content:
        if b.type == "text" and b.text:
            out.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        elif b.type in SERVER_BLOCKS:
            out.append(cast(ContentBlockParam, b.model_dump(mode="json", exclude_none=True)))
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
        summarizer: Summarizer | None = None,
        memory: MemoryService | None = None,
        places: PlaceService | None = None,
        recommend: RecommendService | None = None,
        health: HealthState | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._summarizer = summarizer
        self._health = health
        self._memory = memory
        self._db = db
        self._settings = settings
        self._llm = llm
        self._decisions = decisions
        self._tools = ToolRouter(decisions, memory, places, recommend)
        self._users = list(users)
        self._users_by_id = {u.id: u for u in users}
        self._tz = tz
        self._clock = clock

    async def respond(
        self,
        chat: ChatContext,
        actor: UserRecord,
        text: str,
        *,
        unprompted_reason: str | None = None,
        tier: Tier | None = None,
    ) -> Reply:
        """``unprompted_reason`` is set when the ambient judge (§10.2) decided to step in.
        ``tier`` forces a model tier (``/think``, ``/thinkharder``, §7.1)."""
        try:
            return await self._respond(chat, actor, text, unprompted_reason, tier)
        finally:
            if self._summarizer is not None:
                self._summarizer.schedule(chat.chat_id)

    async def _tier(
        self, text: str, s: RuntimeSettings, forced: Tier | None, unprompted: bool
    ) -> escalation.Choice:
        """§7.1. Unprompted (ambient) replies stay on the default tier; past the budget warn
        ratio everything does, so a "think harder" can't push spend over the cap."""
        if unprompted:
            return escalation.Choice("default", "default")
        choice = escalation.choose(text, s, forced)
        if choice.tier == "default":
            return choice
        st = await budget_status(self._db, s, self._tz)
        if (
            st.daily_spent >= s.budget_warn_ratio * st.daily_cap
            or st.monthly_spent >= s.budget_warn_ratio * st.monthly_cap
        ):
            log.warning("escalation skipped: budget above warning level")
            return escalation.Choice("default", "budget")
        return choice

    async def _respond(
        self,
        chat: ChatContext,
        actor: UserRecord,
        text: str,
        unprompted_reason: str | None,
        forced: Tier | None = None,
    ) -> Reply:
        s = await self._settings.load()
        rows = await self._db.read(
            lambda conn: messages_repo.recent(
                conn, chat.chat_id, s.history_max_turns, only_thread=chat.thread
            )
        )
        summary = await self._db.read(lambda conn: summaries_repo.get(conn, chat.chat_id))
        messages: list[MessageParam] = build_messages(
            rows,
            self._users_by_id,
            is_group=chat.is_group,
            summary=summary.text if summary else None,
        )
        if not messages:
            return Reply(FALLBACK_EMPTY, from_llm=False)
        ctx = TurnContext(
            chat_id=chat.chat_id,
            actor=actor,
            default_for_users=default_for_users(chat, actor),
            tz=self._tz,
            is_group=chat.is_group,
            source=f"telegram:{chat.chat_id}",
        )
        pinned = await self._memory.pinned_block() if self._memory is not None else None
        pets = await self._memory.pets() if self._memory is not None else []
        today = await self._decisions.today(chat.chat_id, self._tz)
        web = await web_status(self._db, s, self._tz, self._health)
        if not web.on and web.reason != "disabled":
            log.info("web tools off", extra={"reason": web.reason})

        def system_for(web_on: bool, handoff: bool = False) -> list[Any]:
            return build_system(
                s.persona_system_prompt,
                dynamic_context(
                    now=self._clock(),
                    actor=actor,
                    users=self._users,
                    is_group=chat.is_group,
                    default_for_users=ctx.default_for_users,
                    unprompted_reason=unprompted_reason,
                    web_paused=web.temporarily_off,
                    today=today,
                    pets=pets,
                    think=ctx.tier != "default",
                ),
                pinned=pinned,
                web=web_on,
                web_handoff=handoff,
            )

        choice = await self._tier(text, s, forced, unprompted_reason is not None)
        ctx.tier = choice.tier
        ctx.max_tokens = None if choice.tier == "default" else s.escalation_max_tokens
        if choice.tier != "default" or choice.reason == "budget":
            log.info("reply tier", extra={"tier": choice.tier, "reason": choice.reason})
        tools: list[ToolUnionParam] = [*self._tools.definitions()]
        try:
            try:
                extra = web_tools(s) if web.on else []
                ctx.web_on = bool(extra)
                # §7.5: below web.tier, offer look_up_web; the web tools (and the stronger model)
                # join the turn only once it's needed.
                handoff = bool(extra) and TIER_RANK[ctx.tier] < TIER_RANK[s.web_tier]
                first = [handoff_tool()] if handoff else extra
                resp = await self._loop(
                    system_for(bool(extra), handoff),
                    messages,
                    ctx,
                    [*tools, *first],
                    handoff=(extra, s.web_tier, s.escalation_max_tokens) if handoff else None,
                )
                if ctx.web_sent and self._health is not None:
                    self._health.web_ok()
            except LLMBadRequest as e:
                if not ctx.web_sent or ctx.web_used:
                    raise
                # e.g. web search disabled for the org in the Console: answer without the web.
                log.warning(
                    "request with web tools rejected; retrying without them",
                    extra={"detail": e.detail or None},
                )
                ctx.web_on = False
                resp = await self._loop(system_for(False), messages, ctx, tools)
                # The same turn went through without them, so the web tools were the problem.
                if self._health is not None:
                    self._health.web_rejected_by_api(e.detail)
        except BudgetExceeded as e:
            log.warning("budget exhausted", extra={"period": e.period})
            reply = await self._fallback(FALLBACK_BUDGET, chat, actor, text, ctx)
            return replace(reply, budget_exhausted=True)
        except LLMError:
            return await self._fallback(FALLBACK_OFFLINE, chat, actor, text, ctx)

        if resp.message.stop_reason == "refusal":
            return Reply("I'd rather not help with that one.", from_llm=False, picks=ctx.last_picks)
        out = resp.text.strip()
        if out and ctx.web_used:
            out = with_sources(out, sources(resp.message))
        if not out:
            if ctx.last_picks:
                out = f"🎲 {bold_list([n for _, n in ctx.last_picks])}"
            elif ctx.recorded:
                # The reaction is the answer (§10.5); nothing to send.
                return Reply("", from_llm=True, recorded=ctx.recorded)
            else:
                return Reply(FALLBACK_EMPTY, from_llm=False)
        return Reply(
            out,
            from_llm=True,
            picks=ctx.last_picks,
            recorded=ctx.recorded,
            recommendation=ctx.recommend is not None and bool(ctx.last_picks),
            tier=ctx.tier,
        )

    async def _loop(
        self,
        system: Any,
        messages: list[MessageParam],
        ctx: TurnContext,
        tools: list[ToolUnionParam],
        handoff: tuple[list[ToolUnionParam], Tier, int] | None = None,
    ) -> LLMResponse:
        """``handoff``: (web tools, tier, max_tokens) to switch to once ``ctx.web_wanted``."""
        ctx.web_sent = ctx.web_sent or (handoff is None and ctx.web_on)
        # After a handoff: what to restore if the API rejects the web tools (tools already ran
        # this turn, so it carries on from here instead of restarting the turn).
        before: tuple[list[ToolUnionParam], Tier, int | None] | None = None
        rejected = ""
        for i in range(MAX_TOOL_ITERATIONS + 1):
            last = i == MAX_TOOL_ITERATIONS
            try:
                resp = await self._call(system, messages, ctx, tools, last)
            except LLMBadRequest as e:
                if before is None or ctx.web_used:
                    raise
                log.warning(
                    "request with web tools rejected; continuing without them",
                    extra={"detail": e.detail or None},
                )
                tools, ctx.tier, ctx.max_tokens = before
                before, rejected = None, e.detail
                ctx.web_on = ctx.web_sent = False
                resp = await self._call(system, messages, ctx, tools, last)
            if rejected and self._health is not None:
                # The same request went through without them, so the web tools were the problem.
                self._health.web_rejected_by_api(rejected)
                rejected = ""
            await self._note_web(resp, ctx)
            if resp.message.stop_reason == "pause_turn" and not last:
                # A long server-tool turn paused; sending it back unchanged resumes it.
                messages = [
                    *messages,
                    {"role": "assistant", "content": assistant_blocks(resp.message.content)},
                ]
                continue
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
            if handoff is not None and ctx.web_wanted:
                web_extra, tier, max_tokens = handoff
                handoff = None
                before = (tools, ctx.tier, ctx.max_tokens)
                tools = [*tools, *web_extra]
                ctx.web_sent = True
                if TIER_RANK[tier] > TIER_RANK[ctx.tier]:
                    ctx.tier, ctx.max_tokens = tier, max_tokens
                log.info("web handoff", extra={"tier": ctx.tier})
        raise AssertionError("unreachable")

    async def _call(
        self,
        system: Any,
        messages: list[MessageParam],
        ctx: TurnContext,
        tools: list[ToolUnionParam],
        last: bool,
    ) -> LLMResponse:
        return await self._llm.complete(
            LLMRequest(
                purpose="chat",
                model_role=ctx.tier,
                system=system,
                messages=messages,
                max_tokens=ctx.max_tokens,
                tools=tools,
                tool_choice={"type": "none"} if last else None,
                user_id=ctx.actor.id,
                chat_id=ctx.chat_id,
                timeout_s=INTERACTIVE_TIMEOUT_S if ctx.tier == "default" else DEEP_TIMEOUT_S,
            )
        )

    async def _note_web(self, resp: LLMResponse, ctx: TurnContext) -> None:
        """Mark the turn as web-assisted and store each search/fetch for the conversation view.
        Failed lookups need no handling here: Claude sees the error result and answers from what
        it has (§7.5), and max_uses stops any retry loop."""
        if not used_web(resp.message.content):
            return
        ctx.web_used = True
        for call in server_calls(resp.message.content):
            log.info("web tool", extra={"tool": call.name, "error": call.error_code or None})
            await self._store_tool_call(ctx.chat_id, call.name, call.input, call.result)

    async def _run_tools(self, resp: LLMResponse, ctx: TurnContext) -> list[ContentBlockParam]:
        results: list[ContentBlockParam] = []
        for block in resp.message.content:
            if block.type != "tool_use":
                continue
            raw = block.input if isinstance(block.input, dict) else {}
            if block.name == HANDOFF_TOOL:
                outcome = self._handoff(ctx)
            else:
                outcome = await self._tools.execute(block.name, raw, ctx)
                rec = ctx.recommend.result if ctx.recommend is not None else None
                if block.name == "find_places" and ctx.web_on and rec and rec.suggest_web:
                    ctx.web_wanted = ctx.web_wanted or rec.slots_left > 0
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

    def _handoff(self, ctx: TurnContext) -> ToolOutcome:
        if not ctx.web_on:
            return ToolOutcome(
                json.dumps({"error": "web lookups are off right now; answer without them"}),
                is_error=True,
            )
        ctx.web_wanted = True
        return ToolOutcome(json.dumps({"ok": True, "note": "web_search and web_fetch are on now."}))

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
