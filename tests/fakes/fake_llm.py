from __future__ import annotations

from collections import deque

from anthropic.types import Message

from app.llm.client import LLMRequest, LLMResponse


def make_message(text: str, *, stop_reason: str = "end_turn", model: str = "fake-model") -> Message:
    return Message.model_validate(
        {
            "id": "msg_fake",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}] if text else [],
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    )


class FakeLLMClient:
    """Scripted replies: each item is reply text or an exception to raise."""

    def __init__(self, *replies: str | Exception) -> None:
        self._replies: deque[str | Exception] = deque(replies)
        self.requests: list[LLMRequest] = []
        self.configured = True

    async def complete(self, req: LLMRequest) -> LLMResponse:
        self.requests.append(req)
        item = self._replies.popleft() if self._replies else "ok"
        if isinstance(item, Exception):
            raise item
        return LLMResponse(message=make_message(item), model="fake-model", cost_usd=0.0)
