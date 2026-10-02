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


def web_turn(
    text: str = "",
    *,
    query: str = "query",
    results: list[tuple[str, str]] | None = None,  # (title, url)
    cite: bool = True,
    error_code: str | None = None,
    fetch_url: str | None = None,
    stop_reason: str = "end_turn",
    then_tool: tuple[str, dict[str, Any]] | None = None,
) -> Message:
    """A response where Claude ran web_search (and optionally web_fetch) server-side (§7.5).
    ``error_code`` makes the search result an error; ``then_tool`` adds a client tool_use."""
    global _ids
    _ids += 1
    sid = f"srvtoolu_{_ids}"
    results = (
        results if results is not None else [("Ramen Keisuke - Eatbook", "https://eatbook.sg/r")]
    )
    search_content: Any = (
        {"type": "web_search_tool_result_error", "error_code": error_code}
        if error_code
        else [
            {
                "type": "web_search_result",
                "title": t,
                "url": u,
                "encrypted_content": "enc",
                "page_age": None,
            }
            for t, u in results
        ]
    )
    content: list[dict[str, Any]] = [
        {"type": "server_tool_use", "id": sid, "name": "web_search", "input": {"query": query}},
        {"type": "web_search_tool_result", "tool_use_id": sid, "content": search_content},
    ]
    if fetch_url:
        _ids += 1
        fid = f"srvtoolu_{_ids}"
        content += [
            {
                "type": "server_tool_use",
                "id": fid,
                "name": "web_fetch",
                "input": {"url": fetch_url},
            },
            {
                "type": "web_fetch_tool_result",
                "tool_use_id": fid,
                "content": {
                    "type": "web_fetch_result",
                    "url": fetch_url,
                    "content": {
                        "type": "document",
                        "source": {"type": "text", "media_type": "text/plain", "data": "page"},
                    },
                    "retrieved_at": "2026-10-02T10:00:00Z",
                },
            },
        ]
    if text:
        citations = (
            [
                {
                    "type": "web_search_result_location",
                    "url": u,
                    "title": t,
                    "encrypted_index": "idx",
                    "cited_text": "good broth",
                }
                for t, u in results
            ]
            if cite and not error_code
            else None
        )
        content.append({"type": "text", "text": text, "citations": citations})
    if then_tool:
        _ids += 1
        content.append(
            {"type": "tool_use", "id": f"toolu_{_ids}", "name": then_tool[0], "input": then_tool[1]}
        )
    return Message.model_validate(
        {
            "id": "msg_fake",
            "type": "message",
            "role": "assistant",
            "model": "fake-model",
            "content": content,
            "stop_reason": "tool_use" if then_tool else stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "server_tool_use": {
                    "web_search_requests": 0 if error_code else 1,
                    "web_fetch_requests": 1 if fetch_url else 0,
                },
            },
        }
    )
