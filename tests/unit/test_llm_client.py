from __future__ import annotations

import json
from typing import Any
from zoneinfo import ZoneInfo

import anthropic
import httpx2
import pytest

from app.db.repos import usage as usage_repo
from app.llm.client import AnthropicLLMClient, BudgetExceeded, LLMRequest, LLMUnavailable
from app.settings import seed_values, set_value
from app.timeutil import to_sql, utcnow
from tests.conftest import Env

TZ = ZoneInfo("Asia/Singapore")


def _ok_body(model: str) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": "Ramen."}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 100,
            "cache_read_input_tokens": 2000,
            "cache_creation_input_tokens": 0,
        },
    }


class Script:
    def __init__(self, *responses: httpx2.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.responses.pop(0)


def _client(env: Env, script: Script, key: str = "sk-test") -> AnthropicLLMClient:
    http = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(script))
    return AnthropicLLMClient(
        api_key=key, db=env.db, settings=env.settings, tz=TZ, http_client=http
    )


def _req(env: Env) -> LLMRequest:
    return LLMRequest(
        purpose="chat",
        model_role="default",
        system=[{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": "dinner?"}],
        user_id=env.jack.id,
        chat_id=-1,
    )


async def test_success_records_usage_and_cost(env: Env) -> None:
    model = seed_values()["models.default"]
    script = Script(httpx2.Response(200, json=_ok_body(model)))
    resp = await _client(env, script).complete(_req(env))
    assert resp.text == "Ramen."

    sent = json.loads(script.requests[0].content)
    assert sent["model"] == model
    assert sent["max_tokens"] == seed_values()["llm.max_tokens"]
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}

    price = seed_values()[f"pricing.{model}"]
    expected = (1000 * price["input"] + 100 * price["output"] + 2000 * price["cache_read"]) / 1e6
    assert resp.cost_usd == pytest.approx(expected)
    row = await env.db.read(lambda c: c.execute("SELECT * FROM usage").fetchone())
    assert (row["purpose"], row["user_id"], row["cache_read_tokens"]) == ("chat", env.jack.id, 2000)
    assert row["cost_usd"] == pytest.approx(expected)


async def test_retries_overloaded_then_succeeds(env: Env) -> None:
    model = seed_values()["models.default"]
    overloaded = httpx2.Response(
        529,
        headers={"retry-after-ms": "5"},
        json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}},
    )
    script = Script(overloaded, httpx2.Response(200, json=_ok_body(model)))
    resp = await _client(env, script).complete(_req(env))
    assert resp.text == "Ramen." and len(script.requests) == 2


async def test_gives_up_after_max_retries(env: Env) -> None:
    err = {"type": "error", "error": {"type": "api_error", "message": "x"}}
    script = Script(*[httpx2.Response(500, headers={"retry-after-ms": "5"}, json=err)] * 3)
    with pytest.raises(LLMUnavailable):
        await _client(env, script).complete(_req(env))
    assert len(script.requests) == 3  # 1 + max_retries(2)
    assert await env.db.read(lambda c: c.execute("SELECT COUNT(*) FROM usage").fetchone()[0]) == 0


async def test_auth_failure_switches_to_fallback(env: Env) -> None:
    err = {"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}
    client = _client(env, Script(httpx2.Response(401, json=err)))
    with pytest.raises(LLMUnavailable):
        await client.complete(_req(env))
    assert not client.configured


async def test_missing_key_never_calls_api(env: Env) -> None:
    script = Script()
    client = _client(env, script, key="")
    assert not client.configured
    with pytest.raises(LLMUnavailable):
        await client.complete(_req(env))
    assert script.requests == []


async def test_budget_exhausted_blocks_call(env: Env) -> None:
    await env.db.write(lambda c: set_value(c, "budget.daily_usd", 0.5))
    row = usage_repo.UsageRow(env.jack.id, "chat", -1, None, "m", 1, 1, 0, 0, 0.6, to_sql(utcnow()))
    await env.db.write(lambda c: usage_repo.insert(c, row))
    script = Script()
    with pytest.raises(BudgetExceeded) as ei:
        await _client(env, script).complete(_req(env))
    assert ei.value.period == "daily"
    assert script.requests == []
