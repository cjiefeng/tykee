"""Scripted Message Batches: ``responder(custom_id, request)`` returns the reply text (or an
Exception to make that request fail). Batches end after ``polls_until_ended`` status checks."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from app.llm.client import BatchItemResult, BatchStatus, LLMRequest, LLMResponse
from tests.fakes.fake_llm import make_message

Responder = Callable[[str, LLMRequest], "str | Exception"]


@dataclass
class FakeBatches:
    responder: Responder = lambda cid, req: (
        '{"episodes": [], "facts": [], "options": [], "skipped_out_of_scope": 0}'
    )
    polls_until_ended: int = 0
    submitted: dict[str, list[tuple[str, LLMRequest]]] = field(default_factory=dict)
    cancelled: list[str] = field(default_factory=list)
    polls: dict[str, int] = field(default_factory=dict)
    fail_submit: Exception | None = None

    async def submit_batch(self, items: Sequence[tuple[str, LLMRequest]]) -> str:
        if self.fail_submit is not None:
            raise self.fail_submit
        batch_id = f"msgbatch_{len(self.submitted) + 1}"
        self.submitted[batch_id] = list(items)
        return batch_id

    async def batch_status(self, batch_id: str) -> BatchStatus:
        self.polls[batch_id] = self.polls.get(batch_id, 0) + 1
        return "ended" if self.polls[batch_id] > self.polls_until_ended else "in_progress"

    async def batch_results(self, batch_id: str, template: LLMRequest) -> list[BatchItemResult]:
        out = []
        for cid, req in self.submitted[batch_id]:
            reply = self.responder(cid, req)
            if isinstance(reply, Exception):
                out.append(BatchItemResult(cid, None, "errored api_error"))
            else:
                out.append(BatchItemResult(cid, LLMResponse(make_message(reply), "fake", 0.0)))
        return out

    async def cancel_batch(self, batch_id: str) -> None:
        self.cancelled.append(batch_id)
