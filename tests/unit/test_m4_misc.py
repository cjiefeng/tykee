"""M4 odds and ends: budget DM, LLM health tracking, log buffer, embedded dashboard server."""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.brain.memory import MemoryPolicyError
from app.dashboard.app import create_app
from app.dashboard.core import DashboardDeps, hash_password
from app.dashboard.server import make_server
from app.health import HealthState
from app.llm.client import BudgetExceeded
from app.logging import LOG_BUFFER, JsonFormatter
from tests.conftest import (
    GROUP_ID,
    JACK_TG,
    TZ,
    Env,
    make_stack,
    mention,
    seed_category,
    tg_message,
)
from tests.fakes.fake_llm import FakeLLMClient


async def test_budget_exhausted_dms_admin_once_per_day(env: Env) -> None:
    stack = make_stack(env, FakeLLMClient(*[BudgetExceeded("daily", 1, 1)] * 3))
    for _ in range(2):
        body, ents = mention("dinner?")
        await stack.adapter.handle_message(tg_message(body, entities=ents), env.jack)
    dms = [s for s in stack.gateway.sent if s.chat_id == JACK_TG]
    assert len(dms) == 1 and "Budget cap reached" in dms[0].text
    stack.health.budget_dm_day = "2000-01-01"  # next day
    body, ents = mention("dinner?")
    await stack.adapter.handle_message(tg_message(body, entities=ents), env.jack)
    assert len([s for s in stack.gateway.sent if s.chat_id == JACK_TG]) == 2


def test_health_error_rate() -> None:
    h = HealthState()
    for ok in (True, True, False, True):
        h.llm_result(ok)
    assert h.llm_error_rate() == (1, 4) and h.last_llm_ok_at is not None


def test_log_ring_buffer() -> None:
    LOG_BUFFER.setFormatter(JsonFormatter())
    logger = logging.getLogger("tykee.test")
    logger.addHandler(LOG_BUFFER)
    try:
        logger.warning("ring buffer works", extra={"k": 1})
    finally:
        logger.removeHandler(LOG_BUFFER)
    assert '"msg": "ring buffer works"' in LOG_BUFFER.lines[-1]


async def test_embedded_server_serves_and_stops(env: Env) -> None:
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
        db_path=env.db.path,
        password_hash=hash_password("x" * 12),
        session_secret="s" * 40,
        log_lines=lambda: [],
    )
    server = make_server(create_app(deps), "127.0.0.1", 0)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    async with httpx.AsyncClient() as c:
        r = await c.get(f"http://127.0.0.1:{port}/healthz")
    assert r.status_code == 200 and r.json()["ok"]
    server.should_exit = True
    await asyncio.wait_for(task, timeout=5)


async def test_dashboard_port_clash_does_not_kill_the_bot(env: Env) -> None:
    import socket

    from app.dashboard.server import serve

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
        db_path=env.db.path,
        password_hash=hash_password("x" * 12),
        session_secret="s" * 40,
        log_lines=lambda: [],
    )
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        server = make_server(create_app(deps), "127.0.0.1", port)
        await asyncio.wait_for(serve(server), timeout=5)  # returns instead of SystemExit


async def test_merge_into_category_that_resolves_back_is_refused(env: Env) -> None:
    import pytest

    from app.decisions import categories as cats

    dinner = await seed_category(env, "dinner", [("Pho", []), ("Laksa", [])])
    supper = await seed_category(env, "supper", [("Pho", [])])
    await env.db.write(lambda c: cats.merge(c, supper, dinner))
    with pytest.raises(ValueError, match="itself"):
        await env.db.write(lambda c: cats.merge(c, dinner, supper))  # stale page
    with pytest.raises(ValueError, match="already merged"):
        await env.db.write(lambda c: cats.merge(c, supper, dinner))
    names = await env.db.read(lambda c: c.execute("SELECT name FROM options").fetchall())
    assert sorted(r[0] for r in names) == ["Laksa", "Pho"]


async def test_failed_approval_goes_back_to_pending(env: Env) -> None:
    import pytest

    stack = make_stack(env)

    async def broken(item: object) -> None:
        raise RuntimeError("boom")

    stack.memory.appliers["category"] = broken
    item = await stack.memory.suggest(
        kind="category", content="c", reason="r", source="s", payload={"phrase": "x"}
    )
    with pytest.raises(RuntimeError):
        await stack.memory.decide(item.id, approve=True, user_id=env.jack.id)
    again = await stack.memory.get(item.id)
    assert again is not None and again.status == "pending"
    # Unknown kinds are an error too, not a silent "approved".
    stack.memory.appliers.clear()
    with pytest.raises(MemoryPolicyError):
        await stack.memory.decide(item.id, approve=True, user_id=env.jack.id)
    assert (await stack.memory.pending())[0].id == item.id
