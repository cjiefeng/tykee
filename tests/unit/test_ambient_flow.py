"""Ambient participation end to end (§10.2): chatter → debounce → rules → judge → reply."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from aiogram.types import MessageReactionUpdated, ReactionTypeEmoji

from app.ambient.judge import JUDGE_SCHEMA
from app.llm.client import BudgetExceeded, LLMUnavailable
from app.settings import set_value
from tests.conftest import (
    GROUP_ID,
    JACK_TG,
    PARTNER_TG,
    Env,
    Stack,
    make_stack,
    mention,
    tg_message,
    tg_user,
)  # fmt: skip
from tests.fakes.fake_llm import FakeLLMClient


def verdict(action: str = "respond", confidence: float = 0.9, reason: str = "they're stuck") -> str:
    return json.dumps({"action": action, "reason": reason, "confidence": confidence})


async def chatter(
    stack: Stack, env: Env, *texts: str, from_id: int = PARTNER_TG, **kw: Any
) -> None:
    actor = env.jack if from_id == JACK_TG else env.partner
    for t in texts:
        await stack.adapter.handle_message(tg_message(t, from_id=from_id, **kw), actor)


async def ambient_log(env: Env) -> list[dict[str, Any]]:
    rows = await env.db.read(
        lambda c: c.execute("SELECT * FROM ambient_log ORDER BY id").fetchall()
    )
    return [dict(r) for r in rows]


async def test_indecision_gets_unprompted_reply(env: Env) -> None:
    llm = FakeLLMClient(verdict(), "Laksa? You haven't had it in ages.")
    stack = make_stack(env, llm)
    await chatter(stack, env, "what to eat ah", "idk anything lah")
    assert stack.gateway.sent == [] and llm.requests == []  # nothing until the burst settles

    await stack.ambient.fire(GROUP_ID)
    judge_req, reply_req = llm.requests
    assert (judge_req.purpose, judge_req.model_role) == ("judge", "judge")
    assert judge_req.json_schema == JUDGE_SCHEMA
    transcript = str(next(iter(judge_req.messages))["content"])
    assert transcript.startswith("Now: Fri 19:00")
    assert re.search(
        r"--- new ---\n\[\d\d:\d\d\] Partner: what to eat ah\n"
        r"\[\d\d:\d\d\] Partner: idk anything lah$",
        transcript,
    )
    assert "Nobody mentioned you. You chose to step in because: they're stuck" in str(
        list(reply_req.system)[-1]["text"]
    )
    sent = stack.gateway.sent[-1]
    assert sent.text == "Laksa? You haven't had it in ages." and sent.reply_to is None
    log = (await ambient_log(env))[-1]
    assert (log["action"], log["confidence"]) == ("respond", 0.9)
    assert log["reply_tg_message_id"] == sent.message_id
    state = await env.db.read(lambda c: c.execute("SELECT * FROM chat_state").fetchone())
    assert state["unprompted_today"] == 1 and state["state_day"] == "2026-10-02"


async def test_low_confidence_or_silent_verdict_stays_quiet(env: Env) -> None:
    llm = FakeLLMClient(verdict(confidence=0.6), verdict("silent", 0.95, "small talk"))
    stack = make_stack(env, llm)
    await chatter(stack, env, "hmm what to eat")
    await stack.ambient.fire(GROUP_ID)
    await chatter(stack, env, "reaching in 5")
    await stack.ambient.fire(GROUP_ID)
    assert stack.gateway.sent == []
    assert [(r["action"], r["confidence"]) for r in await ambient_log(env)] == [
        ("silent", 0.6),
        ("silent", 0.95),
    ]


async def test_cooldown_then_judged_again(env: Env) -> None:
    llm = FakeLLMClient(verdict(), "Ramen!", verdict("silent", 0.9))
    stack = make_stack(env, llm)
    await chatter(stack, env, "idk you decide")
    await stack.ambient.fire(GROUP_ID)
    stack.clock.advance(minutes=10)
    await chatter(stack, env, "still can't decide")
    await stack.ambient.fire(GROUP_ID)
    assert (await ambient_log(env))[-1]["rule"] == "cooldown" and len(llm.requests) == 2
    stack.clock.advance(minutes=11)
    await chatter(stack, env, "ok what now")
    await stack.ambient.fire(GROUP_ID)
    assert len(llm.requests) == 3 and (await ambient_log(env))[-1]["action"] == "silent"


async def test_daily_cap(env: Env) -> None:
    await env.db.write(lambda c: set_value(c, "ambient.max_per_day", 1))
    await env.db.write(lambda c: set_value(c, "ambient.cooldown_min", 0))
    llm = FakeLLMClient(verdict(), "Pho.")
    stack = make_stack(env, llm)
    await chatter(stack, env, "you decide")
    await stack.ambient.fire(GROUP_ID)
    await chatter(stack, env, "you decide again")
    await stack.ambient.fire(GROUP_ID)
    assert (await ambient_log(env))[-1]["rule"] == "daily_cap"
    stack.clock.advance(days=1)
    await chatter(stack, env, "new day, you decide")
    await stack.ambient.fire(GROUP_ID)
    assert len(llm.requests) == 3  # judged again the next day


async def test_media_only_burst_is_skipped(env: Env) -> None:
    stack = make_stack(env)
    await chatter(stack, env, "😂😂")
    await stack.ambient.fire(GROUP_ID)
    assert (await ambient_log(env))[0]["rule"] == "media_only" and stack.llm.requests == []


async def test_mention_cancels_pending_burst_and_skips_rules(env: Env) -> None:
    llm = FakeLLMClient("Sure.")
    stack = make_stack(env, llm)
    await stack.adapter.handle_message(tg_message("/quiet 1h"), env.jack)
    await chatter(stack, env, "what to eat")
    assert stack.ambient._debouncer.pending(GROUP_ID)
    text, ents = mention("pick dinner")
    await stack.adapter.handle_message(tg_message(text, entities=ents), env.jack)
    assert not stack.ambient._debouncer.pending(GROUP_ID)
    assert stack.gateway.sent[-1].text == "Sure."  # muted, but mentions are always answered
    await stack.ambient.fire(GROUP_ID)  # nothing pending: no-op
    assert await ambient_log(env) == []


async def test_quiet_and_unquiet_commands(env: Env) -> None:
    stack = make_stack(env)
    await stack.adapter.handle_message(tg_message("/quiet 90m"), env.jack)
    # Weekday shown when "until" isn't today on the real clock (the stack clock is fixed).
    assert re.match(r"🤐 OK, I'll stay quiet until (\w{3} )?20:30", stack.gateway.sent[-1].text)
    await chatter(stack, env, "idk")
    await stack.ambient.fire(GROUP_ID)
    assert (await ambient_log(env))[-1]["rule"] == "muted"
    await stack.adapter.handle_message(tg_message("/unquiet"), env.jack)
    state = await env.db.read(lambda c: c.execute("SELECT muted_until FROM chat_state").fetchone())
    assert state[0] is None
    await stack.adapter.handle_message(tg_message("/quiet whenever"), env.jack)
    assert stack.gateway.sent[-1].text.startswith("Usage: /quiet")
    dm = tg_message("/quiet", from_id=JACK_TG, chat_id=JACK_TG, chat_type="private")
    await stack.adapter.handle_message(dm, env.jack)
    assert "Quiet mode is for the group" in stack.gateway.sent[-1].text


async def test_mute_phrase(env: Env) -> None:
    stack = make_stack(env)
    await chatter(stack, env, "ok bot shh")
    assert re.match(r"🤐 OK, I'll stay quiet until (\w{3} )?21:00", stack.gateway.sent[-1].text)
    assert not stack.ambient._debouncer.pending(GROUP_ID)


async def _unprompted(stack: Stack, env: Env) -> int:
    await chatter(stack, env, "you decide lah")
    await stack.ambient.fire(GROUP_ID)
    return stack.gateway.sent[-1].message_id


def _reaction(message_id: int, emoji: str, user: int = PARTNER_TG) -> MessageReactionUpdated:
    from datetime import UTC, datetime

    from aiogram.types import Chat

    return MessageReactionUpdated(
        chat=Chat(id=GROUP_ID, type="supergroup"),
        message_id=message_id,
        date=datetime.now(UTC),
        user=tg_user(user),
        old_reaction=[],
        new_reaction=[ReactionTypeEmoji(emoji=emoji)],
    )


async def _multiplier(env: Env) -> float:
    row = await env.db.read(
        lambda c: c.execute("SELECT cooldown_multiplier FROM chat_state").fetchone()
    )
    return float(row[0])


async def test_thumbs_down_doubles_cooldown_once(env: Env) -> None:
    stack = make_stack(env, FakeLLMClient(verdict(), "Chicken rice!"))
    msg_id = await _unprompted(stack, env)
    await stack.adapter.handle_reaction(_reaction(msg_id, "👎"))
    await stack.adapter.handle_reaction(_reaction(msg_id, "👎", user=JACK_TG))
    assert (await ambient_log(env))[-1]["feedback"] == "negative"
    assert await _multiplier(env) == 2.0
    stack.clock.advance(minutes=30)  # 20 min x 2 = still cooling down
    await chatter(stack, env, "idk")
    await stack.ambient.fire(GROUP_ID)
    assert (await ambient_log(env))[-1]["rule"] == "cooldown"


async def test_reaction_on_prompted_message_is_ignored(env: Env) -> None:
    stack = make_stack(env)
    await stack.adapter.handle_reaction(_reaction(12345, "👎"))
    assert await ambient_log(env) == []


async def test_thumbs_up_is_positive(env: Env) -> None:
    stack = make_stack(env, FakeLLMClient(verdict(), "Chicken rice!"))
    msg_id = await _unprompted(stack, env)
    await stack.adapter.handle_reaction(_reaction(msg_id, "👍"))
    assert (await ambient_log(env))[-1]["feedback"] == "positive"


async def test_didnt_ask_is_negative_within_window(env: Env) -> None:
    stack = make_stack(env, FakeLLMClient(verdict(), "Chicken rice!"))
    await _unprompted(stack, env)
    stack.clock.advance(minutes=5)
    await chatter(stack, env, "lol nobody asked")
    assert (await ambient_log(env))[0]["feedback"] == "negative"


async def test_judge_errors_and_budget_mean_silence(env: Env) -> None:
    llm = FakeLLMClient("not json", LLMUnavailable("down"), BudgetExceeded("daily", 1, 1))
    stack = make_stack(env, llm)
    for _ in range(3):
        await chatter(stack, env, "idk")
        await stack.ambient.fire(GROUP_ID)
    assert [r["rule"] for r in await ambient_log(env)] == ["judge_error", "judge_error", "budget"]
    assert stack.gateway.sent == []


async def test_failed_reply_after_respond_verdict_sends_nothing(env: Env) -> None:
    stack = make_stack(env, FakeLLMClient(verdict(), LLMUnavailable("down")))
    await chatter(stack, env, "idk you decide")
    await stack.ambient.fire(GROUP_ID)
    assert stack.gateway.sent == []  # never "my brain's offline" unprompted
    log = (await ambient_log(env))[-1]
    assert (log["action"], log["rule"]) == ("silent", "no_reply")
    state = await env.db.read(lambda c: c.execute("SELECT * FROM chat_state").fetchone())
    assert state is None or state["unprompted_today"] == 0


async def test_debounce_fires_by_itself(env: Env) -> None:
    await env.db.write(lambda c: set_value(c, "ambient.debounce_s", 0.02))
    llm = FakeLLMClient(verdict("silent", 0.9))
    stack = make_stack(env, llm)
    await chatter(stack, env, "what to eat", "idk")
    await asyncio.sleep(0.1)
    assert len(llm.requests) == 1
    log = (await ambient_log(env))[0]
    assert log["to_msg_id"] - log["from_msg_id"] == 1  # one burst covering both messages


async def test_ambient_disabled(env: Env) -> None:
    await env.db.write(lambda c: set_value(c, "ambient.enabled", False))
    stack = make_stack(env)
    await chatter(stack, env, "idk")
    assert not stack.ambient._debouncer.pending(GROUP_ID)


async def test_dms_never_go_through_ambient(env: Env) -> None:
    stack = make_stack(env, FakeLLMClient("Hi!"))
    await chatter(stack, env, "hi", from_id=JACK_TG, chat_id=JACK_TG, chat_type="private")
    assert stack.gateway.sent[-1].text == "Hi!"
    assert not stack.ambient._debouncer.pending(JACK_TG)
