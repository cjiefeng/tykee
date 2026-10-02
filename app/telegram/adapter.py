"""Inbound Telegram handling: persist every allowlisted message, reply when addressed (group:
mention / reply-to-bot / command; DMs: always), commands, and ✅ 🎲 ❌ button callbacks."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from app.db.database import Database
from app.db.repos import messages as messages_repo
from app.db.repos.users import UserRecord
from app.decisions.engine import PickRequest
from app.decisions.service import DecisionService
from app.orchestrator.orchestrator import (
    ChatContext,
    Orchestrator,
    bold_list,
    default_for_users,
)
from app.telegram.addressing import (
    BotIdentity,
    Command,
    classify,
    is_addressed,
    parse_command,
    stored_text,
)
from app.telegram.gateway import ChatGateway
from app.telegram.keyboards import decision_keyboard, parse_callback
from app.telegram.middleware import GROUP_TYPES

log = logging.getLogger(__name__)

HELP_TEXT = """\
I'm **Tykee** 🎲, a decision helper for the two of you.

In the group, mention me or reply to one of my messages. In a DM, just talk to me.

Commands:
/pick <category>: random pick from saved options (e.g. /pick dinner)
/options <category>: list saved options
/help: this message"""


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
        decisions: DecisionService,
        me: BotIdentity,
        users: Sequence[UserRecord],
    ) -> None:
        self._db = db
        self._gateway = gateway
        self._orchestrator = orchestrator
        self._decisions = decisions
        self._me = me
        self._users = list(users)

    def router(self) -> Router:
        router = Router(name="tykee")

        @router.message()
        async def _on_message(message: Message, actor: UserRecord) -> None:
            await self.handle_message(message, actor)

        @router.callback_query(F.data.startswith("d:"))
        async def _on_callback(callback: CallbackQuery, actor: UserRecord) -> None:
            await self.handle_callback(callback, actor)

        return router

    # --- messages ------------------------------------------------------------------------------

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
        chat = ChatContext(chat_id, is_group)
        cmd = parse_command(msg.text, self._me.username)
        if cmd is not None:
            await self._command(cmd, msg, chat, actor)
            return
        async with self._gateway.typing(chat_id):
            reply = await self._orchestrator.respond(chat, actor, text)
        await self._send(
            chat_id,
            reply.text,
            reply_to=msg.message_id if is_group else None,
            picks=reply.picks,
            store=reply.from_llm,
        )

    async def _send(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to: int | None = None,
        picks: Sequence[tuple[int, str]] = (),
        store: bool = True,
    ) -> None:
        ids = await self._gateway.send_text(
            chat_id, text, reply_to=reply_to, keyboard=decision_keyboard(picks)
        )
        if ids and picks:
            await self._decisions.attach_message([d for d, _ in picks], chat_id, ids[-1])
        if not store:
            return
        content = messages_repo.text_content(text)
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

    # --- commands ------------------------------------------------------------------------------

    async def _command(
        self, cmd: Command, msg: Message, chat: ChatContext, actor: UserRecord
    ) -> None:
        if cmd.name == "start":
            await self._send(chat.chat_id, start_text(self._users), store=False)
        elif cmd.name == "pick":
            await self._cmd_pick(cmd.args, chat, actor)
        elif cmd.name == "options":
            await self._cmd_options(cmd.args, chat)
        else:
            await self._send(chat.chat_id, HELP_TEXT, store=False)

    async def _cmd_pick(self, args: str, chat: ChatContext, actor: UserRecord) -> None:
        if not args:
            await self._send(
                chat.chat_id, "Usage: /pick <category>, e.g. /pick dinner", store=False
            )
            return
        category = await self._decisions.lookup(args)
        if category is None:
            await self._send(
                chat.chat_id,
                f"I don't have a category called _{args}_ yet. Ask me in words and I'll set it up.",
                store=False,
            )
            return
        req = PickRequest(category_id=category.id, for_users=default_for_users(chat, actor))
        result = await self._decisions.pick(category, req, asked_by=actor.id, chat_id=chat.chat_id)
        if not result.picks:
            await self._send(
                chat.chat_id, f"No saved options for {category.display_name} yet.", store=False
            )
            return
        picks = [(p.decision_id, p.name) for p in result.picks]
        await self._send(chat.chat_id, f"🎲 {bold_list([n for _, n in picks])}", picks=picks)

    async def _cmd_options(self, args: str, chat: ChatContext) -> None:
        category = await self._decisions.lookup(args) if args else None
        if category is None:
            await self._send(chat.chat_id, "Usage: /options <category>", store=False)
            return
        options = await self._decisions.list_options(category)
        if not options:
            text = f"No saved options for {category.display_name} yet."
        else:
            lines = [f"• {o.name}" + (f" ({', '.join(o.tags)})" if o.tags else "") for o in options]
            text = f"**{category.display_name}** options:\n" + "\n".join(lines)
        await self._send(chat.chat_id, text, store=False)

    # --- button callbacks ----------------------------------------------------------------------

    async def handle_callback(self, cb: CallbackQuery, actor: UserRecord) -> None:
        parsed = parse_callback(cb.data)
        if parsed is None or cb.message is None:
            await self._gateway.answer_callback(cb.id, "That button has expired.")
            return
        decision_id, action = parsed
        chat_id, message_id = cb.message.chat.id, cb.message.message_id

        fb = await self._decisions.feedback(decision_id, action, actor.id)
        remaining = await self._decisions.open_on_message(chat_id, message_id)
        await self._gateway.set_keyboard(chat_id, message_id, decision_keyboard(remaining))
        if not fb.applied:
            await self._gateway.answer_callback(cb.id, "Already sorted 👍")
            return
        log.info("feedback", extra={"action": action, "user": actor.slug, "decision": decision_id})

        if action == "accept":
            await self._gateway.answer_callback(cb.id, "Locked in ✅")
            await self._send(chat_id, f"✅ **{fb.choice_text}** it is.", reply_to=message_id)
        elif action == "reject":
            await self._gateway.answer_callback(cb.id, "Noted, I'll suggest that less 👌")
        else:
            result = await self._decisions.reroll(fb, asked_by=actor.id, chat_id=chat_id)
            if result is None or not result.picks:
                await self._gateway.answer_callback(cb.id, "Nothing else left 🤷")
                await self._send(
                    chat_id, "That's everything I've got for this one 🤷", reply_to=message_id
                )
                return
            await self._gateway.answer_callback(cb.id, "🎲")
            picks = [(p.decision_id, p.name) for p in result.picks]
            await self._send(
                chat_id,
                f"🎲 How about {bold_list([n for _, n in picks])}?",
                reply_to=message_id,
                picks=picks,
            )
