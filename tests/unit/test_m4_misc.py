"""M4 odds and ends: budget DM, LLM health tracking, log buffer, embedded dashboard server."""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.dashboard.app import create_app
from app.dashboard.core import DashboardDeps, hash_password
from app.dashboard.server import make_server
from app.health import HealthState
from app.llm.client import BudgetExceeded
from app.logging import LOG_BUFFER, JsonFormatter
from tests.conftest import GROUP_ID, JACK_TG, TZ, Env, make_stack, mention, tg_message
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
