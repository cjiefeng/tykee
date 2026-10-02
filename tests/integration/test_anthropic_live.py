"""Opt-in: `make test ARGS="-m integration"` with ANTHROPIC_API_KEY exported. Costs ~$0.03."""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo

import pytest

from app.llm.client import AnthropicLLMClient, LLMRequest
from app.orchestrator.web import server_calls, sources, web_tools
from tests.conftest import Env

pytestmark = pytest.mark.integration


async def test_real_call_records_usage(env: Env) -> None:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        pytest.skip("ANTHROPIC_API_KEY not set")
    client = AnthropicLLMClient(
        api_key=key, db=env.db, settings=env.settings, tz=ZoneInfo("Asia/Singapore")
    )
    resp = await client.complete(
        LLMRequest(
            purpose="chat",
            model_role="default",
            system=[{"type": "text", "text": "Reply with one word."}],
            messages=[{"role": "user", "content": "Say hi."}],
            max_tokens=20,
        )
    )
    assert resp.text
    assert resp.cost_usd > 0


async def test_real_web_search_with_seeded_tool_versions(env: Env) -> None:
    """§7.5: the seeded web tool versions work on the seeded default model and are billed."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        pytest.skip("ANTHROPIC_API_KEY not set")
    client = AnthropicLLMClient(
        api_key=key, db=env.db, settings=env.settings, tz=ZoneInfo("Asia/Singapore")
    )
    s = await env.settings.load()
    resp = await client.complete(
        LLMRequest(
            purpose="chat",
            model_role="default",
            system=[{"type": "text", "text": "Answer in one sentence. Search the web first."}],
            messages=[{"role": "user", "content": "What is the weather in Singapore today?"}],
            tools=web_tools(s),
            max_tokens=400,
        )
    )
    calls = server_calls(resp.message.content)
    assert calls and calls[0].name == "web_search"
    assert resp.text and sources(resp.message)
    row = await env.db.read(lambda c: c.execute("SELECT web_search_requests FROM usage").fetchone())
    assert row[0] >= 1
    assert resp.cost_usd >= s.pricing_web_search
