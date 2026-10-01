"""Opt-in: `make test ARGS="-m integration"` with ANTHROPIC_API_KEY exported. Costs < $0.01."""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo

import pytest

from app.llm.client import AnthropicLLMClient, LLMRequest
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
