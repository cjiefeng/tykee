"""M7 web access (§7.5): server tools on the orchestrator call only, gating, sources, memory
safety, pause_turn, usage/cost. All offline: web results are scripted with ``web_turn``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from app.dashboard import queries
from app.db.repos import usage as usage_repo
from app.llm.client import LLMBadRequest
from app.orchestrator.prompt import WEB_RULES
from app.settings import seed_values, set_value
from app.timeutil import to_sql, utcnow
from tests.conftest import JACK_TG, Env, Stack, make_stack, mention, tg_message
from tests.fakes.fake_llm import FakeLLMClient, tool_call, web_turn
from tests.unit.test_llm_client import Script, _client, _ok_body, _req

RAMEN = "is that new ramen place in Tanjong Pagar any good?"


async def _ask(stack: Stack, env: Env, text: str) -> None:
    body, ents = mention(text)
    await stack.adapter.handle_message(tg_message(body, entities=ents, from_id=JACK_TG), env.jack)


def _names(tools: Any) -> list[str]:
    return [t["name"] for t in tools]


def _system_text(llm: FakeLLMClient, i: int = 0) -> str:
    return "\n".join(b["text"] for b in llm.requests[i].system)


async def _set(env: Env, **values: Any) -> None:
    def _w(conn: Any) -> None:
        for key, value in values.items():
            set_value(conn, key.replace("__", "."), value)

    await env.db.write(_w)


async def _usage(env: Env, *, cost: float = 0.0, searches: int = 0) -> None:
    row = usage_repo.UsageRow(
        user_id=None,
        purpose="chat",
        chat_id=None,
        import_job_id=None,
        model="m",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=cost,
        created_at=to_sql(utcnow()),
        web_search_requests=searches,
    )
    await env.db.write(lambda c: usage_repo.insert(c, row))


# --- done-when: a short, cited answer ----------------------------------------------------------


async def test_ramen_question_gets_short_cited_answer(env: Env) -> None:
    llm = FakeLLMClient(web_turn("Solid, rich tonkotsu; queues at lunch.", query="ramen"))
    stack = make_stack(env, llm)
    await _ask(stack, env, RAMEN)

    tools = {t["name"]: dict(t) for t in llm.requests[0].tools}
    seed = seed_values()
    assert tools["web_search"]["type"] == seed["web.tool_versions"]["web_search"]
    assert tools["web_search"]["max_uses"] == seed["web.search_max_uses"]
    assert tools["web_search"]["user_location"]["country"] == "SG"
    assert tools["web_fetch"]["type"] == seed["web.tool_versions"]["web_fetch"]
    assert tools["web_fetch"]["max_uses"] == seed["web.fetch_max_uses"]
    assert "allowed_domains" not in tools["web_search"]
    assert WEB_RULES.strip() in _system_text(llm)

    reply = stack.gateway.sent[-1].text
    assert reply.startswith("Solid, rich tonkotsu")
    assert "[eatbook.sg](https://eatbook.sg/r)" in reply  # source appended from citations

    rows = await env.db.read(
        lambda c: c.execute("SELECT content FROM messages WHERE role = 'tool'").fetchall()
    )
    stored = json.loads(rows[0][0])
    assert stored[0] == {"type": "tool_use", "name": "web_search", "input": {"query": "ramen"}}
    assert stored[1]["content"] == "https://eatbook.sg/r"


async def test_own_links_and_sources_capped(env: Env) -> None:
    results = [(f"Site {i}", f"https://site{i}.com/x") for i in range(4)]
    llm = FakeLLMClient(
        web_turn("Good, see [Eatbook](https://eatbook.sg/r)."),
        web_turn("Mixed reviews.", results=results),
    )
    stack = make_stack(env, llm)
    await _ask(stack, env, RAMEN)
    assert stack.gateway.sent[-1].text == "Good, see [Eatbook](https://eatbook.sg/r)."
    await _ask(stack, env, RAMEN)
    reply = stack.gateway.sent[-1].text
    assert "site0.com" in reply and "site1.com" in reply and "site2.com" not in reply


async def test_search_error_is_logged_and_answered(env: Env) -> None:
    llm = FakeLLMClient(
        web_turn(
            "Couldn't check live info, but it used to be good.", error_code="too_many_requests"
        )
    )
    stack = make_stack(env, llm)
    await _ask(stack, env, RAMEN)
    assert stack.gateway.sent[-1].text.startswith("Couldn't check live info")
    assert len(llm.requests) == 1  # no retry
    (content,) = await env.db.read(
        lambda c: c.execute("SELECT content FROM messages WHERE role = 'tool'").fetchone()
    )
    assert json.loads(content)[1]["content"] == "error: too_many_requests"


# --- memory safety -----------------------------------------------------------------------------


async def test_web_fact_goes_to_inbox_even_with_auto_approve(env: Env) -> None:
    await _set(env, memory__auto_approve=True)
    llm = FakeLLMClient(
        web_turn(
            "Ramen Keisuke closed for good.",
            then_tool=(
                "propose_memory",
                {
                    "owner": "shared",
                    "content": "Ramen Keisuke Tanjong Pagar has closed permanently.",
                    "reason": "Search result",
                    "topic": "food",
                    "source_url": "https://eatbook.sg/r",
                },
            ),
        ),
        tool_call("write_note", path="shared/food", mode="append", content="Keisuke closed."),
        "It's closed, sadly. I've suggested remembering that.",
    )
    stack = make_stack(env, llm)
    await stack.store.ensure_skeleton(env.users)
    await _ask(stack, env, RAMEN)

    proposed = json.loads(llm.tool_results(1)[0]["content"])
    assert proposed["status"] == "pending"
    write = llm.tool_results(2)[0]
    assert write.get("is_error") is True and "propose_memory" in write["content"]
    (item,) = await stack.memory.pending()
    assert item.source == "web:https://eatbook.sg/r"


async def test_source_url_forces_review_without_web_this_turn(env: Env) -> None:
    await _set(env, memory__auto_approve=True)
    llm = FakeLLMClient(
        tool_call(
            "propose_memory",
            owner="shared",
            content="Ramen X moved to Amoy St.",
            reason="link",
            topic="food",
            source_url="https://example.com/x",
        ),
        "Noted for approval.",
    )
    stack = make_stack(env, llm)
    await _ask(stack, env, "ramen x moved, see https://example.com/x")
    assert json.loads(llm.tool_results(1)[0]["content"])["status"] == "pending"


# --- gating ------------------------------------------------------------------------------------


async def test_disabled_means_no_tools_and_no_rules(env: Env) -> None:
    await _set(env, web__enabled=False)
    llm = FakeLLMClient("ok")
    await _ask(make_stack(env, llm), env, RAMEN)
    assert not {"web_search", "web_fetch"} & set(_names(llm.requests[0].tools))
    assert "Web (web_search" not in _system_text(llm)
    assert "paused" not in _system_text(llm)


async def test_daily_search_cap_drops_web_tools(env: Env) -> None:
    await _set(env, web__daily_search_cap=5)
    await _usage(env, searches=5)
    llm = FakeLLMClient("Can't check that right now.")
    await _ask(make_stack(env, llm), env, RAMEN)
    assert "web_search" not in _names(llm.requests[0].tools)
    assert "Live web lookups are paused" in _system_text(llm)


async def test_budget_warn_ratio_disables_web_first(env: Env) -> None:
    await _usage(env, cost=0.85)  # daily cap 1.00, warn ratio 0.8
    llm = FakeLLMClient("Sure, ramen it is.")
    stack = make_stack(env, llm)
    await _ask(stack, env, RAMEN)
    assert "web_search" not in _names(llm.requests[0].tools)
    assert stack.gateway.sent[-1].text == "Sure, ramen it is."  # chat itself still works


async def test_fetch_can_be_turned_off_and_domains_filtered(env: Env) -> None:
    await _set(env, web__fetch_max_uses=0, web__allowed_domains=["Eatbook.sg"])
    llm = FakeLLMClient("ok")
    await _ask(make_stack(env, llm), env, RAMEN)
    tools = {t["name"]: dict(t) for t in llm.requests[0].tools}
    assert "web_fetch" not in tools
    assert tools["web_search"]["allowed_domains"] == ["eatbook.sg"]


async def test_settings_validation(env: Env) -> None:
    with pytest.raises(queries.SettingsError, match="not both"):
        await queries.save_settings(
            env.db, {"web.allowed_domains": ["a.com"], "web.blocked_domains": ["b.com"]}
        )
    with pytest.raises(queries.SettingsError):
        await queries.save_settings(env.db, {"web.allowed_domains": ["https://a.com"]})
    with pytest.raises(queries.SettingsError):
        await queries.save_settings(env.db, {"web.tool_versions": {"web_search": "bing"}})
    with pytest.raises(queries.SettingsError):
        await queries.save_settings(env.db, {"web.user_location": {"country": "Singapore"}})


async def test_rejected_web_request_retries_without_web(env: Env) -> None:
    llm = FakeLLMClient(LLMBadRequest("BadRequestError"), "Probably fine, can't check now.")
    stack = make_stack(env, llm)
    await _ask(stack, env, RAMEN)
    assert "web_search" in _names(llm.requests[0].tools)
    assert "web_search" not in _names(llm.requests[1].tools)
    assert stack.gateway.sent[-1].text == "Probably fine, can't check now."


def test_web_tools_only_on_the_orchestrator() -> None:
    """§7.5: never on judge, summary, harvest or import calls."""
    app = Path(__file__).parents[2] / "app"
    users = [
        p.relative_to(app).as_posix()
        for p in app.rglob("*.py")
        if "web_tools(" in p.read_text(encoding="utf-8")
    ]
    assert sorted(users) == ["orchestrator/orchestrator.py", "orchestrator/web.py"]


# --- server-tool turns -------------------------------------------------------------------------


async def test_pause_turn_resumes_with_blocks_verbatim(env: Env) -> None:
    llm = FakeLLMClient(web_turn(stop_reason="pause_turn"), "Worth a try.")
    stack = make_stack(env, llm)
    await _ask(stack, env, RAMEN)
    assert len(llm.requests) == 2
    last = list(llm.requests[1].messages)[-1]
    assert last["role"] == "assistant"
    blocks = list(last["content"])
    assert [dict(b)["type"] for b in blocks] == ["server_tool_use", "web_search_tool_result"]
    assert dict(blocks[1])["content"][0]["encrypted_content"] == "enc"
    assert stack.gateway.sent[-1].text == "Worth a try."


async def test_mixed_server_and_client_tools_echo_search_results(env: Env) -> None:
    llm = FakeLLMClient(
        web_turn("Let me check my notes too.", then_tool=("search_memory", {"query": "ramen"})),
        "You liked Keisuke before.",
    )
    stack = make_stack(env, llm)
    await stack.store.ensure_skeleton(env.users)
    await _ask(stack, env, RAMEN)
    assistant = list(llm.requests[1].messages)[-2]
    types = [dict(b)["type"] for b in assistant["content"]]
    assert types == ["server_tool_use", "web_search_tool_result", "text", "tool_use"]


# --- usage & cost ------------------------------------------------------------------------------


async def test_usage_records_searches_and_bills_per_search(env: Env) -> None:
    model = seed_values()["models.default"]
    body = _ok_body(model)
    body["usage"]["server_tool_use"] = {"web_search_requests": 2, "web_fetch_requests": 1}
    script = Script(httpx2.Response(200, json=body))
    resp = await _client(env, script).complete(_req(env))

    price = seed_values()[f"pricing.{model}"]
    tokens = (1000 * price["input"] + 100 * price["output"] + 2000 * price["cache_read"]) / 1e6
    assert resp.cost_usd == pytest.approx(tokens + 2 * seed_values()["pricing.web_search"])
    row = await env.db.read(
        lambda c: c.execute("SELECT web_search_requests, web_fetch_requests FROM usage").fetchone()
    )
    assert tuple(row) == (2, 1)


async def test_bad_request_maps_to_llm_bad_request(env: Env) -> None:
    err = {"type": "error", "error": {"type": "invalid_request_error", "message": "web off"}}
    script = Script(httpx2.Response(400, json=err))
    client = _client(env, script)
    with pytest.raises(LLMBadRequest):
        await client.complete(_req(env))
    assert client.configured  # a bad request isn't an auth failure
