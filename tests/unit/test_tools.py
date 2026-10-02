from __future__ import annotations

import json

from app.orchestrator.tools import ToolRouter, TurnContext
from tests.conftest import TZ, Env, make_stack, seed_category


def _ctx(env: Env, default: str = "both") -> TurnContext:
    return TurnContext(chat_id=-1, actor=env.jack, default_for_users=default, tz=TZ)


async def test_definitions_cover_m2_tools(env: Env) -> None:
    router = ToolRouter(make_stack(env).decisions)
    names = [t["name"] for t in router.definitions()]
    assert names == [
        "resolve_category",
        "random_pick",
        "list_options",
        "add_option",
        "record_decision",
        "recent_decisions",
    ]
    pick = next(t for t in router.definitions() if t["name"] == "random_pick")
    assert pick["input_schema"]["properties"]["for_users"]["enum"] == ["jack", "partner", "both"]  # type: ignore[index]


async def test_invalid_input_is_tool_error(env: Env) -> None:
    router = ToolRouter(make_stack(env).decisions)
    out = await router.execute("resolve_category", {"phrase": "x"}, _ctx(env))
    assert out.is_error and "invalid input" in out.content
    out = await router.execute("nope", {}, _ctx(env))
    assert out.is_error


async def test_resolve_then_pick_records_last_picks(env: Env) -> None:
    router = ToolRouter(make_stack(env).decisions)
    ctx = _ctx(env)
    out = await router.execute(
        "resolve_category",
        {"phrase": "dinner", "proposed_slug": "dinner", "description": "d", "proposed_tau_days": 3},
        ctx,
    )
    assert json.loads(out.content)["status"] == "created"
    out = await router.execute(
        "random_pick",
        {"category": "dinner", "extra_candidates": [{"name": "Pho", "tags": ["soup"]}]},
        ctx,
    )
    body = json.loads(out.content)
    assert body["picks"] == [{"name": "Pho", "tags": ["soup"]}] and body["for_users"] == "both"
    assert [name for _, name in ctx.last_picks] == ["Pho"]


async def test_pick_rejects_unknown_category_and_for_users(env: Env) -> None:
    router = ToolRouter(make_stack(env).decisions)
    out = await router.execute("random_pick", {"category": "dinner"}, _ctx(env))
    assert out.is_error and "resolve_category" in out.content
    await seed_category(env, "dinner", [("Pho", [])])
    out = await router.execute("random_pick", {"category": "dinner", "for_users": "bob"}, _ctx(env))
    assert out.is_error


async def test_choose_status_lists_catalog(env: Env) -> None:
    await seed_category(env, "dinner")
    router = ToolRouter(make_stack(env).decisions)
    out = await router.execute(
        "resolve_category",
        {
            "phrase": "board game",
            "proposed_slug": "board-game",
            "description": "g",
            "proposed_tau_days": 7,
        },
        _ctx(env),
    )
    body = json.loads(out.content)
    assert body["status"] == "choose" and body["categories"][0]["slug"] == "dinner"


async def test_add_list_recent(env: Env) -> None:
    await seed_category(env, "dinner")
    router = ToolRouter(make_stack(env).decisions)
    ctx = _ctx(env)
    add = {"category": "dinner", "name": "Pho Hung", "tags": ["vietnamese"]}
    assert json.loads((await router.execute("add_option", add, ctx)).content)["added"] is True
    assert json.loads((await router.execute("add_option", add, ctx)).content)["added"] is False
    listed = json.loads((await router.execute("list_options", {"category": "dinner"}, ctx)).content)
    assert listed["options"] == [{"name": "Pho Hung", "tags": ["vietnamese"], "owner": "shared"}]
    recent = json.loads(
        (await router.execute("recent_decisions", {"category": "dinner"}, ctx)).content
    )
    assert recent["decisions"] == []
