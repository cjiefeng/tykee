"""Stage-2 judge (§10.2): one Haiku-tier call with structured JSON output deciding whether the
bot should speak up in the group."""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from pydantic import BaseModel, ValidationError, field_validator

from app.llm.client import LLMClient, LLMRequest

log = logging.getLogger(__name__)

JUDGE_MAX_TOKENS = 200

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["respond", "silent"]},
        "reason": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["action", "reason", "confidence"],
    "additionalProperties": False,
}


class Verdict(BaseModel):
    action: Literal["respond", "silent"]
    reason: str
    confidence: float

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, v: float) -> float:
        return min(max(v, 0.0), 1.0)


class JudgeError(Exception):
    """The judge returned something unusable; treated as silence."""


class Judge:
    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    async def judge(self, *, prompt: str, context: str, transcript: str, chat_id: int) -> Verdict:
        resp = await self._llm.complete(
            LLMRequest(
                purpose="judge",
                model_role="judge",
                system=[{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": f"{context}\n\nChat:\n{transcript}"}],
                max_tokens=JUDGE_MAX_TOKENS,
                json_schema=JUDGE_SCHEMA,
                chat_id=chat_id,
            )
        )
        if resp.message.stop_reason in ("refusal", "max_tokens"):
            raise JudgeError(f"stop_reason={resp.message.stop_reason}")
        try:
            return Verdict.model_validate(json.loads(resp.text))
        except (json.JSONDecodeError, ValidationError) as e:
            raise JudgeError("invalid judge output") from e
