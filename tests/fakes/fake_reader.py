"""Scripted account reader (§10.7) for service and dashboard tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.reader.models import (
    ChatInfo,
    ChatRef,
    DialogName,
    ReaderChatNotFound,
    ReaderError,
    ReaderMessage,
)


@dataclass
class FakeReader:
    history: dict[int, list[ReaderMessage]] = field(default_factory=dict)  # peer → messages
    entities: dict[str | int, ChatInfo] = field(default_factory=dict)
    dialogs: list[DialogName] = field(default_factory=list)
    fail: ReaderError | None = None  # raised by the next read
    calls: list[tuple[str, object]] = field(default_factory=list)
    logged_out: bool = False
    closed: int = 0

    def _maybe_fail(self) -> None:
        if self.fail is not None:
            err, self.fail = self.fail, None
            raise err

    async def fetch_new(self, chat: ChatRef, min_id: int | None) -> list[ReaderMessage]:
        self.calls.append(("fetch_new", (chat.peer_id, min_id)))
        self._maybe_fail()
        msgs = self.history.get(chat.peer_id, [])
        if min_id is None:
            return msgs[-1:]
        return [m for m in msgs if m.id > min_id]

    async def backfill(
        self, chat: ChatRef, since: datetime, before_id: int | None
    ) -> list[ReaderMessage]:
        self.calls.append(("backfill", (chat.peer_id, before_id)))
        self._maybe_fail()
        return [
            m
            for m in self.history.get(chat.peer_id, [])
            if m.date >= since and (before_id is None or m.id <= before_id)
        ]

    async def list_dialog_names(self, limit: int = 50) -> list[DialogName]:
        self.calls.append(("list_dialog_names", limit))
        self._maybe_fail()
        return self.dialogs[:limit]

    async def resolve(self, ref: str | int) -> ChatInfo:
        self.calls.append(("resolve", ref))
        self._maybe_fail()
        if ref not in self.entities:
            raise ReaderChatNotFound(f"no entity {ref}")
        return self.entities[ref]

    async def log_out(self) -> None:
        self.logged_out = True

    async def close(self) -> None:
        self.closed += 1
