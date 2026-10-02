from __future__ import annotations

from collections import deque
from typing import Any

from anthropic.types import Message

from app.llm.client import LLMRequest, LLMResponse

_ids = 0


def make_message(
    text: str = "",
    *,
    stop_reason: str = "end_turn",
    model: str = "fake-model",
    tool_calls: list[tuple[str, dict[str, Any]]] | None = None,
) -> Message:
    """A Messages API response. ``tool_calls`` → tool_use blocks and stop_reason 'tool_use'."""
    global _ids
    content: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
    for name, args in tool_calls or []:
        _ids += 1
        content.append({"type": "tool_use", "id": f"toolu_{_ids}", "name": name, "input": args})
    return Message.model_validate(
        {
            "id": "msg_fake",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": "tool_use" if tool_calls else stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    )


def tool_call(name: str, **args: Any) -> Message:
    return make_message(tool_calls=[(name, args)])


class FakeLLMClient:
    """Scripted replies: each item is reply text, a prebuilt Message, or an exception to raise."""

    def __init__(self, *replies: str | Message | Exception) -> None:
        self._replies: deque[str | Message | Exception] = deque(replies)
        self.requests: list[LLMRequest] = []
        self.configured = True

    async def complete(self, req: LLMRequest) -> LLMResponse:
        self.requests.append(req)
        item = self._replies.popleft() if self._replies else "ok"
        if isinstance(item, Exception):
            raise item
        msg = item if isinstance(item, Message) else make_message(item)
        return LLMResponse(message=msg, model="fake-model", cost_usd=0.0)

    def tool_results(self, request_index: int) -> list[dict[str, Any]]:
        """tool_result blocks sent in the given request (the last user message)."""
        last = list(self.requests[request_index].messages)[-1]
        content = last["content"]
        assert isinstance(content, list)
        return [dict(b) for b in content if dict(b).get("type") == "tool_result"]
