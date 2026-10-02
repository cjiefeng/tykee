"""Outbound Telegram port. The orchestrator and adapter only talk to ``ChatGateway``; tests use
``tests/fakes/fake_gateway.py``."""

from __future__ import annotations

import logging
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    ReactionTypeEmoji,
    ReplyParameters,
)
from aiogram.utils.chat_action import ChatActionSender

from app.telegram.formatting import to_html_chunks
from app.telegram.keyboards import Keyboard

log = logging.getLogger(__name__)


class ChatGateway(Protocol):
    async def send_text(
        self,
        chat_id: int,
        text: str,
        reply_to: int | None = None,
        keyboard: Keyboard | None = None,
        thread_id: int | None = None,
    ) -> list[int]:
        """Send model/plain text (rendered to HTML, split). The keyboard goes on the last chunk.
        ``thread_id`` is the forum topic's ``message_thread_id`` (None = General / no topics).
        Returns sent message ids."""
        ...

    async def send_html(self, chat_id: int, html: str, thread_id: int | None = None) -> int:
        """Send pre-rendered, already-escaped HTML (≤ 4096 chars)."""
        ...

    async def set_keyboard(self, chat_id: int, message_id: int, keyboard: Keyboard | None) -> None:
        """Replace (or remove, with None) the inline keyboard on a sent message."""
        ...

    async def answer_callback(self, callback_id: str, text: str) -> None: ...

    async def set_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        """React to a message (§10.5: a recorded place is confirmed quietly). Best effort."""
        ...

    def typing(
        self, chat_id: int, thread_id: int | None = None
    ) -> AbstractAsyncContextManager[object]: ...

    async def leave_chat(self, chat_id: int) -> None: ...


def _markup(keyboard: Keyboard | None) -> InlineKeyboardMarkup | None:
    if not keyboard:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=b.text, callback_data=b.data) for b in row]
            for row in keyboard
        ]
    )


class AiogramGateway:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send_text(
        self,
        chat_id: int,
        text: str,
        reply_to: int | None = None,
        keyboard: Keyboard | None = None,
        thread_id: int | None = None,
    ) -> list[int]:
        chunks = to_html_chunks(text)
        ids: list[int] = []
        for i, chunk in enumerate(chunks):
            reply = (
                ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
                if reply_to is not None and i == 0
                else None
            )
            msg = await self._bot.send_message(
                chat_id,
                chunk,
                reply_parameters=reply,
                message_thread_id=thread_id,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                reply_markup=_markup(keyboard) if i == len(chunks) - 1 else None,
            )
            ids.append(msg.message_id)
        return ids

    async def send_html(self, chat_id: int, html: str, thread_id: int | None = None) -> int:
        msg = await self._bot.send_message(chat_id, html, message_thread_id=thread_id)
        return msg.message_id

    async def set_keyboard(self, chat_id: int, message_id: int, keyboard: Keyboard | None) -> None:
        try:
            await self._bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=_markup(keyboard)
            )
        except TelegramBadRequest as e:
            # "message is not modified" and edits on very old messages are harmless.
            log.debug("edit_message_reply_markup failed", extra={"error": e.message})

    async def answer_callback(self, callback_id: str, text: str) -> None:
        try:
            await self._bot.answer_callback_query(callback_id, text=text)
        except TelegramAPIError:
            log.debug("answer_callback_query failed")

    async def set_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        try:
            await self._bot.set_message_reaction(
                chat_id, message_id, reaction=[ReactionTypeEmoji(emoji=emoji)]
            )
        except TelegramAPIError as e:
            log.warning("set_message_reaction failed", extra={"error": str(e)})

    def typing(
        self, chat_id: int, thread_id: int | None = None
    ) -> AbstractAsyncContextManager[object]:
        return ChatActionSender.typing(chat_id=chat_id, bot=self._bot, message_thread_id=thread_id)

    async def leave_chat(self, chat_id: int) -> None:
        try:
            await self._bot.leave_chat(chat_id)
        except TelegramAPIError:
            log.warning("leave_chat failed", extra={"chat_id": chat_id})
