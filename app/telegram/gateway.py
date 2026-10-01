"""Outbound Telegram port. The orchestrator and adapter only talk to ``ChatGateway``; tests use
``tests/fakes/fake_gateway.py``."""

from __future__ import annotations

import logging
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import LinkPreviewOptions, ReplyParameters
from aiogram.utils.chat_action import ChatActionSender

from app.telegram.formatting import to_html_chunks

log = logging.getLogger(__name__)


class ChatGateway(Protocol):
    async def send_text(self, chat_id: int, text: str, reply_to: int | None = None) -> list[int]:
        """Send model/plain text (rendered to HTML, split). Returns sent message ids."""
        ...

    async def send_html(self, chat_id: int, html: str) -> int:
        """Send pre-rendered, already-escaped HTML (≤ 4096 chars)."""
        ...

    def typing(self, chat_id: int) -> AbstractAsyncContextManager[object]: ...

    async def leave_chat(self, chat_id: int) -> None: ...


class AiogramGateway:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send_text(self, chat_id: int, text: str, reply_to: int | None = None) -> list[int]:
        ids: list[int] = []
        for i, chunk in enumerate(to_html_chunks(text)):
            reply = (
                ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
                if reply_to is not None and i == 0
                else None
            )
            msg = await self._bot.send_message(
                chat_id,
                chunk,
                reply_parameters=reply,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            ids.append(msg.message_id)
        return ids

    async def send_html(self, chat_id: int, html: str) -> int:
        msg = await self._bot.send_message(chat_id, html)
        return msg.message_id

    def typing(self, chat_id: int) -> AbstractAsyncContextManager[object]:
        return ChatActionSender.typing(chat_id=chat_id, bot=self._bot)

    async def leave_chat(self, chat_id: int) -> None:
        try:
            await self._bot.leave_chat(chat_id)
        except TelegramAPIError:
            log.warning("leave_chat failed", extra={"chat_id": chat_id})
