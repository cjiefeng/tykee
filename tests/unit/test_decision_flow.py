"""End-to-end through adapter → orchestrator tool loop → engine, with a scripted Claude."""

from __future__ import annotations

import json
from typing import Any

from aiogram.types import CallbackQuery

from app.llm.client import LLMUnavailable
from app.orchestrator.orchestrator import MAX_TOOL_ITERATIONS
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
)  # fmt: skip
from tests.fakes.fake_llm import FakeLLMClient, make_message, tool_call

DINNER = {
    "phrase": "dinner",
    "proposed_slug": "dinner",
    "description": "what to eat for dinner",
    "proposed_tau_days": 3,
}
MOVIE = {
    "phrase": "what movie",
    "proposed_slug": "movie",
    "description": "which film to watch",
    "proposed_tau_days": 21,
}


async def _ask(stack: Stack, env: Env, text: str, from_id: int = JACK_TG) -> None:
    body, ents = mention(text)
    actor = env.jack if from_id == JACK_TG else env.partner
    await stack.adapter.handle_message(tg_message(body, entities=ents, from_id=from_id), actor)


async def _decisions(env: Env) -> list[dict[str, Any]]:
    rows = await env.db.read(lambda c: c.execute("SELECT * FROM decisions ORDER BY id").fetchall())
    return [dict(r) for r in rows]


def _cb(data: str, message_id: int, from_id: int = JACK_TG) -> CallbackQuery:
    msg = tg_message("bot reply", from_id=BOT.id, message_id=message_id)
    return CallbackQuery(
        id="cb", from_user=tg_user(from_id), chat_instance="ci", data=data, message=msg
    )


async def test_done_when_dinner_and_movie_create_categories_and_pick(env: Env) -> None:
    llm = FakeLLMClient(
        tool_call("resolve_category", **DINNER),
        tool_call(
            "random_pick",
            category="dinner",
            extra_candidates=[
                {"name": "Pho", "tags": ["soup"]},
                {"name": "Laksa", "tags": ["spicy"]},
            ],
        ),
        "Pho tonight, it's raining.",
        tool_call("resolve_category", **MOVIE),
        tool_call("resolve_category", **MOVIE, create_new=True),
        tool_call(
            "random_pick", category="movie", extra_candidates=[{"name": "Arrival", "tags": []}]
        ),
        "Arrival.",
    )
    stack = make_stack(env, llm)
    await _ask(stack, env, "dinner?")
    await _ask(stack, env, "what movie?", from_id=PARTNER_TG)

    slugs = await env.db.read(lambda c: [r[0] for r in c.execute("SELECT slug FROM categories")])
    assert slugs == ["dinner", "movie"]
    # Claude saw 'created', then 'choose' with dinner listed, then 'created' for movie.
    assert json.loads(llm.tool_results(1)[0]["content"])["status"] == "created"
    choose = json.loads(llm.tool_results(4)[0]["content"])
    assert choose["status"] == "choose" and [c["slug"] for c in choose["categories"]] == ["dinner"]

    sent = stack.gateway.sent
    assert [s.text for s in sent] == ["Pho tonight, it's raining.", "Arrival."]
    assert all(s.keyboard is not None and len(s.keyboard[0]) == 3 for s in sent)
    ds = await _decisions(env)
    assert [(d["choice_text"], d["status"], d["tg_message_id"]) for d in ds] == [
        (ds[0]["choice_text"], "suggested", sent[0].message_id),
        ("Arrival", "suggested", sent[1].message_id),
    ]
    assert ds[0]["choice_text"] in {"Pho", "Laksa"} and ds[1]["for_users"] == "both"

    # tool calls are stored for debugging but never replayed
    roles = await env.db.read(lambda c: [r[0] for r in c.execute("SELECT role FROM messages")])
    assert roles.count("tool") == 5
    replayed = llm.requests[3].messages
    assert all(isinstance(m["content"], str) for m in replayed)


async def test_tool_loop_is_capped(env: Env) -> None:
    looping = [tool_call("list_options", category="x") for _ in range(MAX_TOOL_ITERATIONS)]
    llm = FakeLLMClient(*looping, "Giving up gracefully.")
    stack = make_stack(env, llm)
    await _ask(stack, env, "loop forever")
    assert len(llm.requests) == MAX_TOOL_ITERATIONS + 1
    assert llm.requests[-1].tool_choice == {"type": "none"}
    assert llm.requests[0].tool_choice is None
    assert stack.gateway.sent[-1].text == "Giving up gracefully."


async def test_accept_callback(env: Env) -> None:
    await seed_category(env, "dinner", [("Pho", [])])
    stack = make_stack(env)
    await stack.adapter.handle_message(tg_message("/pick dinner"), env.jack)
    pick_msg = stack.gateway.sent[-1]
    assert pick_msg.text == "🎲 **Pho**" and pick_msg.keyboard is not None
    did = (await _decisions(env))[0]["id"]

    await stack.adapter.handle_callback(
        _cb(callback_data(did, "accept"), pick_msg.message_id), env.partner
    )
    assert stack.gateway.toasts == ["Locked in ✅"]
    assert stack.gateway.keyboards[pick_msg.message_id] is None
    assert stack.gateway.sent[-1].text == "✅ **Pho** it is."
    assert (await _decisions(env))[0]["status"] == "accepted"

    await stack.adapter.handle_callback(
        _cb(callback_data(did, "reject"), pick_msg.message_id), env.jack
    )
    assert stack.gateway.toasts[-1] == "Already sorted 👍"


async def test_reroll_callback_posts_new_pick_without_llm(env: Env) -> None:
    await seed_category(env, "dinner", [("Pho", []), ("Laksa", [])])
    stack = make_stack(env)
    await stack.adapter.handle_message(tg_message("/pick dinner"), env.jack)
    first = stack.gateway.sent[-1]
    d0 = (await _decisions(env))[0]
    await stack.adapter.handle_callback(
        _cb(callback_data(d0["id"], "reroll"), first.message_id), env.jack
    )

    ds = await _decisions(env)
    assert [d["status"] for d in ds] == ["rerolled", "suggested"]
    assert ds[1]["choice_text"] != d0["choice_text"]
    assert stack.gateway.sent[-1].text == f"🎲 How about **{ds[1]['choice_text']}**?"
    assert stack.gateway.sent[-1].keyboard is not None and stack.llm.requests == []

    # rerolling the last remaining option: nothing left this session
    await stack.adapter.handle_callback(
        _cb(callback_data(ds[1]["id"], "reroll"), stack.gateway.sent[-1].message_id), env.jack
    )
    assert stack.gateway.toasts[-1] == "Nothing else left 🤷"


async def test_reject_one_of_several_keeps_other_rows(env: Env) -> None:
    await seed_category(env, "dinner", [("Pho", []), ("Laksa", []), ("Ramen", [])])
    llm = FakeLLMClient(tool_call("random_pick", category="dinner", n=2), "Two ideas.")
    stack = make_stack(env, llm)
    await _ask(stack, env, "give me 2 dinner ideas")
    msg = stack.gateway.sent[-1]
    assert msg.keyboard is not None and len(msg.keyboard) == 2
    first_id = (await _decisions(env))[0]["id"]
    await stack.adapter.handle_callback(
        _cb(callback_data(first_id, "reject"), msg.message_id), env.jack
    )
    remaining = stack.gateway.keyboards[msg.message_id]
    assert remaining is not None and len(remaining) == 1


async def test_commands_options_and_unknown_category(env: Env) -> None:
    await seed_category(env, "dinner", [("Pho", ["soup"])])
    stack = make_stack(env)
    await stack.adapter.handle_message(tg_message("/options dinner"), env.jack)
    assert stack.gateway.sent[-1].text == "**Dinner** options:\n• Pho (soup)"
    await stack.adapter.handle_message(tg_message("/pick bowling"), env.jack)
    assert "don't have a category" in stack.gateway.sent[-1].text
    assert stack.llm.requests == []


async def test_fallback_picks_by_alias_when_claude_is_down(env: Env) -> None:
    await seed_category(env, "dinner", [("Pho", [])])
    stack = make_stack(env, FakeLLMClient(LLMUnavailable("down")))
    await _ask(stack, env, "what's for dinner tonight?")
    sent = stack.gateway.sent[-1]
    assert "random dinner pick: **Pho**" in sent.text and sent.keyboard is not None
    assert (await _decisions(env))[0]["tg_message_id"] == sent.message_id


async def test_fallback_without_alias_suggests_pick_command(env: Env) -> None:
    stack = make_stack(env, FakeLLMClient(LLMUnavailable("down")))
    await _ask(stack, env, "help me choose")
    assert stack.gateway.sent[-1].text.endswith("Try /pick <category>.")


async def test_llm_failure_after_pick_still_delivers_pick(env: Env) -> None:
    await seed_category(env, "dinner", [("Pho", [])])
    llm = FakeLLMClient(tool_call("random_pick", category="dinner"), LLMUnavailable("down"))
    stack = make_stack(env, llm)
    await _ask(stack, env, "dinner?")
    sent = stack.gateway.sent[-1]
    assert "**Pho**" in sent.text and sent.keyboard is not None


async def test_empty_final_text_falls_back_to_pick_names(env: Env) -> None:
    await seed_category(env, "dinner", [("Pho", [])])
    llm = FakeLLMClient(tool_call("random_pick", category="dinner"), make_message(""))
    stack = make_stack(env, llm)
    await _ask(stack, env, "dinner?")
    assert stack.gateway.sent[-1].text == "🎲 **Pho**"


async def test_dm_defaults_to_sender(env: Env) -> None:
    await seed_category(env, "dinner", [("Pho", [])])
    llm = FakeLLMClient(tool_call("random_pick", category="dinner"), "Pho.")
    stack = make_stack(env, llm)
    dm = tg_message("dinner?", from_id=PARTNER_TG, chat_id=PARTNER_TG, chat_type="private")
    await stack.adapter.handle_message(dm, env.partner)
    assert (await _decisions(env))[0]["for_users"] == "partner"
    system = llm.requests[0].system
    assert list(system)[-1]["text"].endswith("Default for_users: partner")
    assert GROUP_ID != PARTNER_TG


def test_assistant_blocks_strip_response_only_fields() -> None:
    from app.orchestrator.orchestrator import assistant_blocks

    msg = make_message("thinking out loud", tool_calls=[("list_options", {"category": "dinner"})])
    blocks = assistant_blocks(msg.content)
    assert blocks[0] == {"type": "text", "text": "thinking out loud"}
    assert set(blocks[1]) == {"type", "id", "name", "input"}
