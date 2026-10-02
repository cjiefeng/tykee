"""M3 end to end: memory tools in the Claude loop, constraints on picks, inbox, decision log."""

from __future__ import annotations

import json
from collections import Counter

from aiogram.types import CallbackQuery

from app.settings import set_value
from app.telegram.keyboards import callback_data
from tests.conftest import (
    BOT,
    GROUP_ID,
    JACK_TG,
    PARTNER_TG,
    Env,
    Stack,
    make_stack,
    mention,
    seed_category,
    tg_message,
    tg_user,
)
from tests.fakes.fake_llm import FakeLLMClient, tool_call


async def _ask(stack: Stack, env: Env, text: str, from_id: int = JACK_TG) -> None:
    body, ents = mention(text)
    actor = env.jack if from_id == JACK_TG else env.partner
    await stack.adapter.handle_message(tg_message(body, entities=ents, from_id=from_id), actor)


def _cb(data: str, message_id: int, from_id: int = JACK_TG) -> CallbackQuery:
    msg = tg_message("bot", from_id=BOT.id, message_id=message_id)
    return CallbackQuery(
        id="cb", from_user=tg_user(from_id), chat_instance="ci", data=data, message=msg
    )


async def test_done_when_remember_coriander_changes_picks(env: Env) -> None:
    await seed_category(
        env,
        "dinner",
        [("Pho", ["soup", "contains:coriander"]), ("Laksa", ["spicy"]), ("Chicken rice", [])],
    )
    llm = FakeLLMClient(
        tool_call("read_note", path="people/jack"),
        tool_call(
            "write_note",
            path="people/jack",
            mode="append",
            heading="Constraints",
            content="Hates coriander (cilantro).",
            add_avoid_tags=["contains:coriander"],
        ),
        "Noted, no more coriander for you.",
    )
    stack = make_stack(env, llm)
    await stack.store.ensure_skeleton(env.users)
    await _ask(stack, env, "remember I hate coriander")

    assert stack.gateway.sent[-1].text == "Noted, no more coriander for you."
    jack = await stack.store.read("people/jack")
    assert jack is not None and jack.avoid_tags == ["contains:coriander"]
    assert "Hates coriander (cilantro)." in jack.body
    read_result = json.loads(llm.tool_results(1)[0]["content"])
    assert read_result["path"] == "people/jack.md" and read_result["pinned"] is True

    # Future picks: Pho is never chosen, in the group (both) or for Jack alone.
    picks: Counter[str] = Counter()
    for _ in range(40):
        await stack.adapter.handle_message(tg_message("/pick dinner"), env.partner)
        picks[stack.gateway.sent[-1].text] += 1
    assert "🎲 **Pho**" not in picks and len(picks) == 2

    # And Claude sees the constraint in its pinned context on the next turn.
    llm2 = FakeLLMClient("ok")
    stack2 = make_stack(env, llm2)
    await _ask(stack2, env, "anything")
    pinned = [b["text"] for b in llm2.requests[0].system if b.get("cache_control")][-1]
    assert "Avoid tags (enforced in code): contains:coriander" in pinned


async def test_constraint_only_applies_to_whom_the_decision_is_for(env: Env) -> None:
    await seed_category(env, "dinner", [("Pho", ["contains:coriander"])])
    stack = make_stack(
        env, FakeLLMClient(tool_call("random_pick", category="dinner", for_users="partner"), "Pho!")
    )
    await stack.store.ensure_skeleton(env.users)
    await stack.memory.write(
        "people/jack", mode="append", content="x", add_avoid_tags=["contains:coriander"]
    )
    await _ask(stack, env, "dinner for partner only")
    result = json.loads(stack.llm.tool_results(1)[0]["content"])
    assert result["picks"] == [{"name": "Pho", "tags": ["contains:coriander"]}]
    assert "excluded_by_constraints" not in result


async def test_pick_tool_reports_constraints_and_filters_extras(env: Env) -> None:
    await seed_category(env, "dinner")
    llm = FakeLLMClient(
        tool_call(
            "random_pick",
            category="dinner",
            extra_candidates=[
                {"name": "Satay", "tags": ["contains:peanut"]},
                {"name": "Fish soup", "tags": []},
            ],
        ),
        "Fish soup.",
    )
    stack = make_stack(env, llm)
    await stack.store.ensure_skeleton(env.users)
    await stack.memory.write(
        "people/partner", mode="append", content="x", add_avoid_tags=["contains:peanut"]
    )
    await _ask(stack, env, "dinner?")
    result = json.loads(llm.tool_results(1)[0]["content"])
    assert result["picks"][0]["name"] == "Fish soup"
    assert result["excluded_by_constraints"] == ["contains:peanut"]


async def test_search_memory_tool_defaults_scope_by_chat(env: Env) -> None:
    llm = FakeLLMClient(tool_call("search_memory", query="laksa"), "ok")
    stack = make_stack(env, llm)
    await stack.store.write("memories/partner/food", mode="create", content="Loves laksa.")
    await _ask(stack, env, "what does partner like?")
    group = json.loads(llm.tool_results(1)[0]["content"])
    assert group["scope"] == "both" and group["results"][0]["path"] == "memories/partner/food.md"

    llm_dm = FakeLLMClient(tool_call("search_memory", query="laksa"), "ok")
    dm_stack = make_stack(env, llm_dm)
    dm = tg_message(
        "what does partner like?", from_id=JACK_TG, chat_id=JACK_TG, chat_type="private"
    )
    await dm_stack.adapter.handle_message(dm, env.jack)
    private = json.loads(llm_dm.tool_results(1)[0]["content"])
    assert private["scope"] == "me" and private["results"] == []


async def test_write_note_policy_error_goes_back_to_claude(env: Env) -> None:
    llm = FakeLLMClient(
        tool_call("write_note", path="logs/hack", mode="create", content="x"), "Sorry."
    )
    stack = make_stack(env, llm)
    await _ask(stack, env, "remember this")
    result = llm.tool_results(1)[0]
    assert result.get("is_error") and "can't write" in result["content"]


async def test_propose_memory_then_admin_approves_in_telegram(env: Env) -> None:
    llm = FakeLLMClient(
        tool_call(
            "propose_memory",
            owner="partner",
            content="Off seafood this month.",
            reason="she said so",
            topic="food",
        ),
        "Got it.",
    )
    stack = make_stack(env, llm)
    await _ask(stack, env, "btw I'm off seafood this month", from_id=PARTNER_TG)
    assert json.loads(llm.tool_results(1)[0]["content"])["status"] == "pending"

    await stack.adapter.handle_message(tg_message("/inbox", from_id=PARTNER_TG), env.partner)
    assert stack.gateway.sent[-1].text == "Only the admin can review the memory inbox."

    await stack.adapter.handle_message(tg_message("/inbox"), env.jack)
    card = stack.gateway.sent[-1]
    assert "Off seafood this month." in card.text and card.keyboard is not None
    item_id = (await stack.memory.pending())[0].id

    await stack.adapter.handle_inbox_callback(
        _cb(f"m:{item_id}:a", card.message_id, PARTNER_TG), env.partner
    )
    assert stack.gateway.toasts[-1] == "Only the admin can approve memories."
    await stack.adapter.handle_inbox_callback(_cb(f"m:{item_id}:a", card.message_id), env.jack)
    assert (
        stack.gateway.toasts[-1] == "Saved ✅" and stack.gateway.keyboards[card.message_id] is None
    )
    note = await stack.store.read("memories/partner/food")
    assert note is not None and "- Off seafood this month." in note.body
    await stack.adapter.handle_inbox_callback(_cb(f"m:{item_id}:x", card.message_id), env.jack)
    assert stack.gateway.toasts[-1] == "Already sorted 👍"
    await stack.adapter.handle_message(tg_message("/inbox"), env.jack)
    assert stack.gateway.sent[-1].text == "📥 Memory inbox is empty."


async def test_auto_approve_saves_immediately(env: Env) -> None:
    await env.db.write(lambda c: set_value(c, "memory.auto_approve", True))
    llm = FakeLLMClient(
        tool_call(
            "propose_memory", owner="jack", content="Likes teh peng.", reason="r", topic="drinks"
        ),
        "ok",
    )
    stack = make_stack(env, llm)
    await _ask(stack, env, "teh peng is the best")
    assert json.loads(llm.tool_results(1)[0]["content"])["status"] == "approved"
    assert await stack.store.exists("memories/jack/drinks")


async def test_accept_mirrors_decision_to_vault_log(env: Env) -> None:
    await seed_category(env, "dinner", [("Pho", [])])
    stack = make_stack(env)
    await stack.adapter.handle_message(tg_message("/pick dinner"), env.jack)
    msg = stack.gateway.sent[-1]
    did = (await env.db.read(lambda c: c.execute("SELECT id FROM decisions").fetchone()))[0]
    await stack.adapter.handle_callback(
        _cb(callback_data(did, "accept"), msg.message_id), env.partner
    )
    log = (env.vault / "logs/2026/10/2026-10-02.md").read_text()
    assert "Dinner · **Pho** · for both · ✅ by Partner" in log


async def test_remember_command_goes_to_claude(env: Env) -> None:
    llm = FakeLLMClient("Saved.")
    stack = make_stack(env, llm)
    await stack.adapter.handle_message(tg_message("/remember I hate coriander"), env.jack)
    assert len(llm.requests) == 1 and stack.gateway.sent[-1].text == "Saved."
    first = next(iter(llm.requests[0].messages))
    assert "/remember I hate coriander" in str(first["content"])


async def test_judge_sees_pinned_constraints(env: Env) -> None:
    llm = FakeLLMClient(json.dumps({"action": "silent", "reason": "x", "confidence": 0.9}))
    stack = make_stack(env, llm)
    await stack.store.ensure_skeleton(env.users)
    await stack.memory.write("people/jack", mode="append", content="Allergic to peanuts.")
    await stack.adapter.handle_message(
        tg_message("satay tonight?", from_id=PARTNER_TG), env.partner
    )
    await stack.ambient.fire(GROUP_ID)
    assert "Allergic to peanuts." in str(next(iter(llm.requests[0].messages))["content"])


async def test_tool_definitions_include_memory_tools(env: Env) -> None:
    llm = FakeLLMClient("ok")
    stack = make_stack(env, llm)
    await _ask(stack, env, "hi")
    names = [t["name"] for t in llm.requests[0].tools]
    assert names[-4:] == ["search_memory", "read_note", "write_note", "propose_memory"]
