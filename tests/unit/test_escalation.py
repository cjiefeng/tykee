"""Model escalation (§7.1): /think → Sonnet-tier, /thinkharder and "think even harder" →
Opus-tier, phrases and long messages, budget fallback, ambient replies stay default."""

from __future__ import annotations

import pytest

from app.db.repos import usage as usage_repo
from app.orchestrator import escalation
from app.settings import RuntimeSettings, set_value
from app.timeutil import to_sql, utcnow
from tests.conftest import JACK_TG, Env, make_stack, mention, tg_message
from tests.fakes.fake_llm import FakeLLMClient


async def settings(env: Env) -> RuntimeSettings:
    return await env.settings.load()


@pytest.mark.parametrize(
    ("text", "tier"),
    [
        ("dinner?", "default"),
        ("think hard: where should we go for our anniversary", "escalated"),
        ("Help us plan Saturday", "escalated"),
        ("ok think even harder about it", "deep"),
        ("Think harder pls", "deep"),
        ("I'm thinking hardware store", "default"),  # whole phrases only
        ("x" * 600, "escalated"),
    ],
)
async def test_choose(env: Env, text: str, tier: str) -> None:
    assert escalation.choose(text, await settings(env)).tier == tier


async def test_commands_and_switch(env: Env) -> None:
    s = await settings(env)
    assert escalation.choose("dinner?", s, "escalated") == escalation.Choice("escalated", "command")
    assert escalation.choose("dinner?", s, "deep").tier == "deep"
    # /think plus a deep phrase lifts it to the deep tier.
    assert escalation.choose("/think even harder", s, "escalated").tier == "deep"
    off = s.model_copy(update={"escalation_enabled": False})
    assert escalation.choose("think hard", off).tier == "default"
    assert escalation.choose("think hard", off, "escalated").tier == "escalated"


def test_deep_model_falls_back_to_escalated() -> None:
    from app.settings import Models

    m = Models(default="h", escalated="s", judge="h", import_extract="o", import_consolidate="o")
    assert m.for_role("deep") == "s"
    assert m.model_copy(update={"deep": "o"}).for_role("deep") == "o"


async def _ask(env: Env, text: str, llm: FakeLLMClient) -> None:
    stack = make_stack(env, llm)
    dm = tg_message(text, from_id=JACK_TG, chat_id=JACK_TG, chat_type="private")
    await stack.adapter.handle_message(dm, env.jack)


async def test_think_commands_route_the_reply(env: Env) -> None:
    llm = FakeLLMClient("Sure.", "Deep answer.", "Plain.")
    await _ask(env, "/think where to go on Saturday", llm)
    await _ask(env, "/thinkharder plan our trip", llm)
    await _ask(env, "dinner?", llm)
    roles = [(r.model_role, r.max_tokens) for r in llm.requests]
    assert roles == [("escalated", 1200), ("deep", 1200), ("default", None)]
    thinking = ["think this through" in str(r.system) for r in llm.requests]
    assert thinking == [True, True, False]


async def test_phrase_in_group_mention_goes_deep(env: Env) -> None:
    llm = FakeLLMClient("ok")
    stack = make_stack(env, llm)
    text, ents = mention("think even harder: Japan or Korea in December?")
    await stack.adapter.handle_message(tg_message(text, entities=ents), env.jack)
    assert llm.requests[-1].model_role == "deep"


async def test_think_without_question_shows_usage(env: Env) -> None:
    llm = FakeLLMClient()
    stack = make_stack(env, llm)
    dm = tg_message("/think", from_id=JACK_TG, chat_id=JACK_TG, chat_type="private")
    await stack.adapter.handle_message(dm, env.jack)
    assert stack.gateway.sent[-1].text == "Usage: /think <question>" and llm.requests == []


async def test_budget_warning_keeps_the_default_model(env: Env) -> None:
    await env.db.write(lambda c: set_value(c, "budget.daily_usd", 1.0))
    row = usage_repo.UsageRow(
        env.jack.id, "chat", None, None, "m", 1, 1, 0, 0, 0.85, to_sql(utcnow())
    )
    await env.db.write(lambda c: usage_repo.insert(c, row))
    llm = FakeLLMClient("ok")
    await _ask(env, "/thinkharder big question", llm)
    assert llm.requests[-1].model_role == "default" and llm.requests[-1].max_tokens is None


async def test_unprompted_replies_stay_default(env: Env) -> None:
    from app.orchestrator.orchestrator import ChatContext
    from tests.conftest import GROUP_ID

    llm = FakeLLMClient("ok")
    stack = make_stack(env, llm)
    await stack.adapter.handle_message(tg_message("think hard about dinner"), env.jack)
    chat = ChatContext(GROUP_ID, True)
    await stack.orchestrator.respond(
        chat, env.jack, "think hard about dinner", unprompted_reason="stuck"
    )
    assert llm.requests[-1].model_role == "default"


async def test_behaviour_page_saves_escalation(env: Env) -> None:
    from app.dashboard.app import create_app
    from app.dashboard.core import DashboardDeps
    from tests.conftest import GROUP_ID, TZ
    from tests.unit.test_dashboard import _HASH, Dash, _client

    stack = make_stack(env)
    deps = DashboardDeps(
        db=env.db,
        settings=env.settings,
        store=stack.store,
        memory=stack.memory,
        decisions=stack.decisions,
        topics=stack.topics,
        health=stack.health,
        gateway=stack.gateway,
        users=env.users,
        tz=TZ,
        group_id=lambda: GROUP_ID,
        db_path=env.vault / "x.db",
        password_hash=_HASH,
        session_secret="s" * 40,
        log_lines=lambda: [],
    )
    d = Dash(_client(create_app(deps)), stack, deps)
    await d.login()
    assert "models.deep" in (await d.client.get("/behaviour")).text
    await d.post(
        "/behaviour/settings",
        {
            "models.deep": "claude-opus-x",
            "escalation.deep_phrases": "go full brain\nthink even harder",
            "escalation.long_message_chars": "0",
        },
    )
    s = await env.settings.load()
    assert s.models.deep == "claude-opus-x" and s.escalation_long_message_chars == 0
    assert s.escalation_deep_phrases == ["go full brain", "think even harder"]
    assert s.escalation_enabled is False  # unticked box
    assert escalation.choose("ok go full brain", s).tier == "default"  # phrases off
    on = s.model_copy(update={"escalation_enabled": True})
    assert escalation.choose("ok go full brain", on).tier == "deep"
