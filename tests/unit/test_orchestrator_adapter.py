from __future__ import annotations

from app.llm.client import BudgetExceeded, LLMUnavailable
from app.orchestrator.orchestrator import FALLBACK_BUDGET, FALLBACK_OFFLINE
from app.telegram.adapter import TelegramAdapter
from tests.conftest import GROUP_ID, JACK_TG, PARTNER_TG, Env, make_stack, mention, tg_message
from tests.fakes.fake_gateway import FakeGateway
from tests.fakes.fake_llm import FakeLLMClient


def _adapter(env: Env, llm: FakeLLMClient) -> tuple[TelegramAdapter, FakeGateway]:
    stack = make_stack(env, llm)
    return stack.adapter, stack.gateway


async def _rows(env: Env) -> list[tuple[str, int | None]]:
    rows = await env.db.read(
        lambda c: c.execute("SELECT role, user_id FROM messages ORDER BY id").fetchall()
    )
    return [(r["role"], r["user_id"]) for r in rows]


async def test_group_chatter_is_persisted_but_not_answered(env: Env) -> None:
    llm = FakeLLMClient()
    adapter, gw = _adapter(env, llm)
    await adapter.handle_message(tg_message("what to eat", from_id=JACK_TG), env.jack)
    await adapter.handle_message(tg_message("idk", from_id=PARTNER_TG), env.partner)
    assert gw.sent == [] and llm.requests == []
    assert await _rows(env) == [("user", env.jack.id), ("user", env.partner.id)]


async def test_mention_gets_reply_with_group_history(env: Env) -> None:
    llm = FakeLLMClient("Ramen tonight.")
    adapter, gw = _adapter(env, llm)
    await adapter.handle_message(tg_message("what to eat", from_id=PARTNER_TG), env.partner)
    text, ents = mention("pick one")
    trigger = tg_message(text, entities=ents, from_id=JACK_TG)
    await adapter.handle_message(trigger, env.jack)

    assert gw.typing_in == [GROUP_ID]
    assert [(s.chat_id, s.text, s.reply_to) for s in gw.sent] == [
        (GROUP_ID, "Ramen tonight.", trigger.message_id)
    ]
    req = llm.requests[0]
    assert req.messages == [
        {"role": "user", "content": "[Partner] what to eat\n[Jack] @TykeeBot pick one"}
    ]
    assert req.user_id == env.jack.id and req.chat_id == GROUP_ID
    assert (await _rows(env))[-1] == ("assistant", None)


async def test_duplicate_delivery_is_ignored(env: Env) -> None:
    llm = FakeLLMClient()
    adapter, gw = _adapter(env, llm)
    text, ents = mention("hi")
    msg = tg_message(text, entities=ents, message_id=4242)
    await adapter.handle_message(msg, env.jack)
    await adapter.handle_message(msg, env.jack)
    assert len(gw.sent) == 1 and len(llm.requests) == 1


async def test_dm_always_answered_without_reply_quote(env: Env) -> None:
    llm = FakeLLMClient("Sure.")
    adapter, gw = _adapter(env, llm)
    dm = tg_message("hello", from_id=JACK_TG, chat_id=JACK_TG, chat_type="private")
    await adapter.handle_message(dm, env.jack)
    assert [(s.chat_id, s.reply_to) for s in gw.sent] == [(JACK_TG, None)]
    assert llm.requests[0].messages == [{"role": "user", "content": "hello"}]


async def test_commands_do_not_call_llm(env: Env) -> None:
    llm = FakeLLMClient()
    adapter, gw = _adapter(env, llm)
    await adapter.handle_message(tg_message("/start@TykeeBot"), env.jack)
    await adapter.handle_message(tg_message("/start@OtherBot"), env.jack)
    assert llm.requests == [] and len(gw.sent) == 1
    assert "can see all conversations" in gw.sent[0].text


async def test_llm_failures_fall_back_and_are_not_stored(env: Env) -> None:
    llm = FakeLLMClient(LLMUnavailable("down"), BudgetExceeded("daily", 1.0, 1.0))
    adapter, gw = _adapter(env, llm)
    for _ in range(2):
        text, ents = mention("dinner?")
        await adapter.handle_message(tg_message(text, entities=ents), env.jack)
    assert [s.text.split(" Try")[0] for s in gw.sent] == [FALLBACK_OFFLINE, FALLBACK_BUDGET]
    assert all(role in ("user", "tool") for role, _ in await _rows(env))


async def test_history_window_respects_setting(env: Env) -> None:
    from app.settings import set_value

    await env.db.write(lambda c: set_value(c, "history.max_turns", 2))
    llm = FakeLLMClient()
    adapter, _ = _adapter(env, llm)
    for i in range(5):
        await adapter.handle_message(tg_message(f"m{i}"), env.jack)
    text, ents = mention("now")
    await adapter.handle_message(tg_message(text, entities=ents), env.jack)
    assert llm.requests[0].messages == [
        {"role": "user", "content": "[Jack] m4\n[Jack] @TykeeBot now"}
    ]
