from __future__ import annotations

import json
from typing import Any
from zoneinfo import ZoneInfo

import anthropic
import httpx2
import pytest

from app.db.repos import usage as usage_repo
from app.llm.client import (
    AnthropicLLMClient,
    BudgetExceeded,
    LLMBadRequest,
    LLMRequest,
    LLMUnavailable,
)
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


async def test_bad_request_carries_the_api_message(env: Env) -> None:
    err = {"type": "error", "error": {"type": "invalid_request_error", "message": "web off"}}
    script = Script(httpx2.Response(400, json=err))
    with pytest.raises(LLMBadRequest) as info:
        await _client(env, script).complete(_req(env))
    assert info.value.detail == "web off"


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


async def test_json_schema_becomes_output_config(env: Env) -> None:
    model = seed_values()["models.judge"]
    script = Script(httpx2.Response(200, json=_ok_body(model)))
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    req = _req(env)
    req.model_role = "judge"
    req.json_schema = schema
    await _client(env, script).complete(req)
    sent = json.loads(script.requests[0].content)
    assert sent["output_config"] == {"format": {"type": "json_schema", "schema": schema}}
    assert "tool_choice" not in sent and "tools" not in sent


# --- Message Batches and streaming (bootstrap import, §15.3) ---------------------------------


def _batch(status: str = "in_progress") -> dict[str, Any]:
    return {
        "id": "msgbatch_1",
        "type": "message_batch",
        "processing_status": status,
        "request_counts": {"processing": 0, "succeeded": 1, "errored": 1, "canceled": 0,
                           "expired": 0},
        "created_at": "2026-10-02T00:00:00Z",
        "expires_at": "2026-10-03T00:00:00Z",
        "ended_at": None,
        "archived_at": None,
        "cancel_initiated_at": None,
        "results_url": "https://api.anthropic.com/v1/messages/batches/msgbatch_1/results",
    }  # fmt: skip


def _import_req(env: Env, job_id: int) -> LLMRequest:
    return LLMRequest(
        purpose="import_extract",
        model_role="import_extract",
        system=[{"type": "text", "text": "rules"}],
        messages=[{"role": "user", "content": "chat"}],
        max_tokens=16000,
        json_schema={"type": "object"},
        import_job_id=job_id,
    )


async def _job(env: Env) -> int:
    return await env.db.write(
        lambda c: int(
            c.execute(
                "INSERT INTO import_jobs(source, file_sha256, since, status) "
                "VALUES ('telegram', 'abc', '2026-04-01', 'extracting')"
            ).lastrowid
            or 0
        )
    )


async def test_batch_submit_status_and_results_record_discounted_usage(env: Env) -> None:
    model = seed_values()["models.import_extract"]
    job_id = await _job(env)
    jsonl = "\n".join(
        json.dumps(line)
        for line in [
            {"custom_id": "w1", "result": {"type": "succeeded", "message": _ok_body(model)}},
            {"custom_id": "w2", "result": {"type": "errored", "error": {"type": "error",
             "error": {"type": "api_error", "message": "x"}}}},
        ]
    )  # fmt: skip
    script = Script(
        httpx2.Response(200, json=_batch()),
        httpx2.Response(200, json=_batch("ended")),
        httpx2.Response(200, json=_batch("ended")),
        httpx2.Response(200, text=jsonl, headers={"content-type": "application/x-jsonl"}),
    )
    client = _client(env, script)
    req = _import_req(env, job_id)
    assert await client.submit_batch([("w1", req), ("w2", req)]) == "msgbatch_1"
    body = json.loads(script.requests[0].content)
    assert [r["custom_id"] for r in body["requests"]] == ["w1", "w2"]
    params = body["requests"][0]["params"]
    assert params["model"] == model and params["max_tokens"] == 16000
    assert params["output_config"]["format"]["type"] == "json_schema"

    assert await client.batch_status("msgbatch_1") == "ended"
    results = await client.batch_results("msgbatch_1", req)
    assert [(r.custom_id, r.response is not None) for r in results] == [("w1", True), ("w2", False)]
    assert "api_error" in results[1].error
    price = seed_values()[f"pricing.{model}"]
    full = (1000 * price["input"] + 100 * price["output"] + 2000 * price["cache_read"]) / 1e6
    row = await env.db.read(lambda c: c.execute("SELECT * FROM usage").fetchone())
    assert (row["purpose"], row["import_job_id"]) == ("import_extract", job_id)
    assert row["cost_usd"] == pytest.approx(full / 2)


async def test_import_calls_ignore_the_daily_cap_but_not_the_monthly(env: Env) -> None:
    job_id = await _job(env)
    await env.db.write(lambda c: set_value(c, "budget.daily_usd", 0.5))
    row = usage_repo.UsageRow(None, "chat", -1, None, "m", 1, 1, 0, 0, 0.6, to_sql(utcnow()))
    await env.db.write(lambda c: usage_repo.insert(c, row))
    script = Script(httpx2.Response(200, json=_batch()))
    client = _client(env, script)
    await client.submit_batch([("w1", _import_req(env, job_id))])  # daily cap doesn't apply
    with pytest.raises(BudgetExceeded):
        await client.complete(_req(env))  # but chat is still capped
    await env.db.write(lambda c: set_value(c, "budget.monthly_usd", 0.5))
    with pytest.raises(BudgetExceeded) as ei:
        await client.submit_batch([("w1", _import_req(env, job_id))])
    assert ei.value.period == "monthly"


async def test_stream_request_uses_sse_and_records_usage(env: Env) -> None:
    model = seed_values()["models.import_consolidate"]
    events = [
        ("message_start", {"type": "message_start", "message": {**_ok_body(model), "content": [],
         "stop_reason": None, "usage": {"input_tokens": 50, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": '{"ok": true}'}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn",
         "stop_sequence": None}, "usage": {"output_tokens": 20}}),
        ("message_stop", {"type": "message_stop"}),
    ]  # fmt: skip
    sse = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)
    script = Script(httpx2.Response(200, text=sse, headers={"content-type": "text/event-stream"}))
    req = _import_req(env, await _job(env))
    req.purpose, req.model_role, req.stream = "import_consolidate", "import_consolidate", True
    resp = await _client(env, script).complete(req)
    assert resp.text == '{"ok": true}'
    assert json.loads(script.requests[0].content)["stream"] is True
    row = await env.db.read(lambda c: c.execute("SELECT * FROM usage").fetchone())
    assert (row["input_tokens"], row["output_tokens"]) == (50, 20)
