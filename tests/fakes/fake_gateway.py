from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field

from app.telegram.keyboards import Keyboard


@dataclass
class Sent:
    chat_id: int
    text: str
    reply_to: int | None = None
    html: bool = False
    keyboard: Keyboard | None = None
    message_id: int = 0


@dataclass
class FakeGateway:
    sent: list[Sent] = field(default_factory=list)
    left: list[int] = field(default_factory=list)
    typing_in: list[int] = field(default_factory=list)
    keyboards: dict[int, Keyboard | None] = field(default_factory=dict)  # message_id → current
    toasts: list[str] = field(default_factory=list)
    _next_id: int = 1000

    async def send_text(
        self,
        chat_id: int,
        text: str,
        reply_to: int | None = None,
        keyboard: Keyboard | None = None,
    ) -> list[int]:
        self._next_id += 1
        self.sent.append(Sent(chat_id, text, reply_to, keyboard=keyboard, message_id=self._next_id))
        self.keyboards[self._next_id] = keyboard
        return [self._next_id]

    async def send_html(self, chat_id: int, html: str) -> int:
        self._next_id += 1
        self.sent.append(Sent(chat_id, html, html=True, message_id=self._next_id))
        return self._next_id

    async def set_keyboard(self, chat_id: int, message_id: int, keyboard: Keyboard | None) -> None:
        self.keyboards[message_id] = keyboard

    async def answer_callback(self, callback_id: str, text: str) -> None:
        self.toasts.append(text)

    def typing(self, chat_id: int) -> AbstractAsyncContextManager[object]:
        @asynccontextmanager
        async def _cm() -> AsyncIterator[object]:
            self.typing_in.append(chat_id)
            yield None

        return _cm()

    async def leave_chat(self, chat_id: int) -> None:
        self.left.append(chat_id)
