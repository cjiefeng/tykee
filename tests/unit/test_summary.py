"""Rolling chat summaries (§7.2)."""

from __future__ import annotations

import asyncio

from app.llm.client import LLMUnavailable
from app.settings import set_value
from tests.conftest import GROUP_ID, PARTNER_TG, Env, make_stack, mention, tg_message
from tests.fakes.fake_llm import FakeLLMClient


async def _setup(env: Env, turns: int = 2, batch: int = 3) -> None:
    await env.db.write(lambda c: set_value(c, "history.max_turns", turns))
    await env.db.write(lambda c: set_value(c, "summary.batch", batch))


async def _summary(env: Env) -> tuple[str, int] | None:
    row = await env.db.read(lambda c: c.execute("SELECT * FROM chat_summaries").fetchone())
    return (row["summary"], row["upto_msg_id"]) if row else None


async def test_summarises_messages_that_left_the_window(env: Env) -> None:
    await _setup(env)
    llm = FakeLLMClient("They argued about laksa vs pho.")
    stack = make_stack(env, llm)
    for i in range(6):
        await stack.adapter.handle_message(tg_message(f"m{i}", from_id=PARTNER_TG), env.partner)
    await stack.summarizer.run(GROUP_ID)

    req = llm.requests[0]
    assert (req.purpose, req.model_role) == ("summary", "default")
    content = str(next(iter(req.messages))["content"])
    assert "Previous summary:\n(none yet)" in content
    assert all(f"Partner: m{i}" in content for i in range(4))
    assert "m4" not in content and "m5" not in content  # still inside the replay window
    assert await _summary(env) == ("They argued about laksa vs pho.", 4)


async def test_no_call_until_batch_is_reached(env: Env) -> None:
    await _setup(env, turns=2, batch=10)
    stack = make_stack(env)
    for i in range(6):
        await stack.adapter.handle_message(tg_message(f"m{i}"), env.jack)
    await stack.summarizer.run(GROUP_ID)
    assert stack.llm.requests == [] and await _summary(env) is None


async def test_summary_is_replayed_and_extended(env: Env) -> None:
    await _setup(env)
    llm = FakeLLMClient("S1", "Reply.", "S2")
    stack = make_stack(env, llm)
    for i in range(6):
        await stack.adapter.handle_message(tg_message(f"m{i}"), env.jack)
    await stack.summarizer.run(GROUP_ID)

    text, ents = mention("so?")
    await stack.adapter.handle_message(tg_message(text, entities=ents), env.jack)
    first_turn = str(next(iter(llm.requests[1].messages))["content"])
    assert first_turn.startswith("(Summary of the earlier conversation: S1)")

    # the reply scheduled a background run; more messages → the summary is extended
    for i in range(6, 9):
        await stack.adapter.handle_message(tg_message(f"m{i}"), env.jack)
    await stack.summarizer.run(GROUP_ID)
    assert "Previous summary:\nS1" in str(next(iter(llm.requests[-1].messages))["content"])
    assert (await _summary(env) or ("", 0))[0] == "S2"


async def test_reply_schedules_background_summary(env: Env) -> None:
    await _setup(env, turns=1, batch=1)
    llm = FakeLLMClient("Reply.", "Summary text")
    stack = make_stack(env, llm)
    await stack.adapter.handle_message(tg_message("before"), env.jack)
    text, ents = mention("hi")
    await stack.adapter.handle_message(tg_message(text, entities=ents), env.jack)
    await asyncio.gather(*stack.summarizer._running.values())
    assert (await _summary(env) or ("", 0))[0] == "Summary text"


async def test_llm_failure_keeps_old_summary(env: Env) -> None:
    await _setup(env)
    stack = make_stack(env, FakeLLMClient(LLMUnavailable("down")))
    for i in range(6):
        await stack.adapter.handle_message(tg_message(f"m{i}"), env.jack)
    await stack.summarizer.run(GROUP_ID)
    assert await _summary(env) is None
