"""Inbound Telegram handling: persist every allowlisted message, reply when addressed
(M1: mention / reply-to-bot / command in the group; always in DMs)."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from aiogram import Router
from aiogram.types import Message

from app.db.database import Database
from app.db.repos import messages as messages_repo
from app.db.repos.users import UserRecord
from app.orchestrator.orchestrator import ChatContext, Orchestrator
from app.telegram.addressing import (
    BotIdentity,
    Command,
    classify,
    is_addressed,
    parse_command,
    stored_text,
)
from app.telegram.gateway import ChatGateway
from app.telegram.middleware import GROUP_TYPES

log = logging.getLogger(__name__)

HELP_TEXT = """\
I'm **Tykee** 🎲, a decision helper for the two of you.

In the group, mention me or reply to one of my messages. In a DM, just talk to me.

Commands:
/help: this message
More commands (/pick, /options, /remember, /forget, /think, /quiet) are coming."""


def start_text(users: Sequence[UserRecord]) -> str:
    admin = next((u.display_name for u in users if u.is_admin), "the admin")
    return (
        f"{HELP_TEXT}\n\n"
        f"Privacy: {admin} (admin) can see all conversations and memories in the dashboard. "
        "Messages are sent to the Anthropic API only when I reply."
    )


class TelegramAdapter:
    def __init__(
        self,
        *,
        db: Database,
        gateway: ChatGateway,
        orchestrator: Orchestrator,
        me: BotIdentity,
        users: Sequence[UserRecord],
    ) -> None:
        self._db = db
        self._gateway = gateway
        self._orchestrator = orchestrator
        self._me = me
        self._users = list(users)

    def router(self) -> Router:
        router = Router(name="tykee")

        @router.message()
        async def _on_message(message: Message, actor: UserRecord) -> None:
            await self.handle_message(message, actor)

        return router

    async def handle_message(self, msg: Message, actor: UserRecord) -> None:
        chat_id = msg.chat.id
        is_group = msg.chat.type in GROUP_TYPES
        kind = classify(msg)
        text = stored_text(msg, kind)
        content = messages_repo.text_content(text)
        row_id = await self._db.write(
            lambda conn: messages_repo.insert(
                conn,
                chat_id=chat_id,
                tg_message_id=msg.message_id,
                user_id=actor.id,
                role="user",
                kind=kind,
                content=content,
            )
        )
        if row_id is None:
            return  # duplicate delivery

        if is_group and not is_addressed(msg, self._me):
            return
        log.info("addressed", extra={"chat_id": chat_id, "user": actor.slug, "group": is_group})
        cmd = parse_command(msg.text, self._me.username)
        if cmd is not None:
            await self._command(cmd, msg)
            return
        await self._reply(msg, actor, is_group)

    async def _command(self, cmd: Command, msg: Message) -> None:
        if cmd.name == "start":
            await self._gateway.send_text(msg.chat.id, start_text(self._users))
        else:
            await self._gateway.send_text(msg.chat.id, HELP_TEXT)

    async def _reply(self, msg: Message, actor: UserRecord, is_group: bool) -> None:
        chat_id = msg.chat.id
        async with self._gateway.typing(chat_id):
            reply = await self._orchestrator.respond(ChatContext(chat_id, is_group), actor)
        ids = await self._gateway.send_text(
            chat_id, reply.text, reply_to=msg.message_id if is_group else None
        )
        if not reply.from_llm:
            return
        content = messages_repo.text_content(reply.text)
        await self._db.write(
            lambda conn: messages_repo.insert(
                conn,
                chat_id=chat_id,
                tg_message_id=ids[0] if ids else None,
                user_id=None,
                role="assistant",
                kind="text",
                content=content,
            )
        )
