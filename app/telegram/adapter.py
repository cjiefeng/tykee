"""Inbound Telegram handling: persist every allowlisted message, reply when addressed (group:
mention / reply-to-bot / command; DMs: always), commands, and ✅ 🎲 ❌ button callbacks."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message, MessageReactionUpdated, ReactionTypeEmoji

from app.ambient.phrases import parse_duration
from app.ambient.service import AmbientService
from app.brain.memory import MemoryService
from app.db.database import Database
from app.db.repos import messages as messages_repo
from app.db.repos.users import UserRecord
from app.decisions.engine import PickRequest
from app.decisions.service import DecisionService
from app.health import HealthState
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
from app.telegram.keyboards import (
    decision_keyboard,
    inbox_keyboard,
    parse_callback,
    parse_inbox_callback,
)
from app.telegram.middleware import GROUP_TYPES
from app.telegram.topics import TopicService, send_thread, thread_of

log = logging.getLogger(__name__)

HELP_TEXT = """\
I'm **Tykee** 🎲, a decision helper for the two of you.

In the group, mention me or reply to one of my messages. In a DM, just talk to me.

Commands:
/pick <category>: random pick from saved options (e.g. /pick dinner)
/options <category>: list saved options
/remember <fact>: save something to memory
/forget <fact>: remove something from memory
/inbox: review memories waiting for approval (admin)
/settopic: make the current topic the one I answer in (admin, forum groups)
/quiet [2h]: in the group, don't chime in unprompted for a while (default 2h)
/unquiet: allow chiming in again
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
        ambient: AmbientService,
        me: BotIdentity,
        users: Sequence[UserRecord],
        tz: ZoneInfo,
        memory: MemoryService | None = None,
        topics: TopicService | None = None,
        health: HealthState | None = None,
    ) -> None:
        self._topics = topics
        self._health = health
        self._memory = memory
        self._ambient = ambient
        self._tz = tz
        ambient.responder = self.respond_unprompted
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

        @router.callback_query(F.data.startswith("m:"))
        async def _on_inbox_callback(callback: CallbackQuery, actor: UserRecord) -> None:
            await self.handle_inbox_callback(callback, actor)

        @router.message_reaction()
        async def _on_reaction(reaction: MessageReactionUpdated, actor: UserRecord) -> None:
            await self.handle_reaction(reaction)

        return router

    # --- messages ------------------------------------------------------------------------------

    async def handle_message(self, msg: Message, actor: UserRecord) -> None:
        chat_id = msg.chat.id
        is_group = msg.chat.type in GROUP_TYPES
        thread = thread_of(msg)
        gate = "answer"
        if is_group and self._topics is not None:
            if await self._topics.learn_from(msg):
                return  # topic created/renamed/closed: bookkeeping, not chat
            gate = await self._topics.gate(thread)
            if gate == "drop":
                return  # ignored topic (§10.4): never stored, never sent anywhere
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
                thread_id=thread,
            )
        )
        if row_id is None:
            return  # duplicate delivery

        cmd = parse_command(msg.text, self._me.username)
        if is_group and cmd is not None and cmd.name == "settopic":
            await self._cmd_settopic(msg, actor, thread)
            return
        if gate == "offtopic":
            # Stored for the harvester; Tykee never speaks outside the answer topic.
            if is_addressed(msg, self._me):
                await self._off_topic(chat_id, thread)
            return

        addressed = not is_group or is_addressed(msg, self._me)
        if is_group and kind == "text":
            if await self._ambient.is_mute_request(text):
                until = await self._ambient.mute(chat_id)
                await self._send(chat_id, self._quiet_text(until), store=False, thread=thread)
                return
            await self._ambient.check_negative_text(chat_id, text)
        if not addressed:
            await self._ambient.on_chatter(chat_id, row_id, actor, kind, text)
            return
        if is_group:
            self._ambient.cancel(chat_id)
        log.info("addressed", extra={"chat_id": chat_id, "user": actor.slug, "group": is_group})
        chat = ChatContext(chat_id, is_group, thread=await self._history_thread(chat_id, is_group))
        if cmd is not None and cmd.name not in ("remember", "forget"):
            await self._command(cmd, msg, chat, actor, thread)
            return
        # /remember and /forget go to Claude as-is; the rules tell it to use write_note.
        async with self._gateway.typing(chat_id, send_thread(thread)):
            reply = await self._orchestrator.respond(chat, actor, text)
        await self._send(
            chat_id,
            reply.text,
            reply_to=msg.message_id if is_group else None,
            picks=reply.picks,
            store=reply.from_llm,
            thread=thread,
        )

    async def _history_thread(self, chat_id: int, is_group: bool) -> int | None:
        if not is_group or self._topics is None:
            return None
        return await self._topics.history_thread(chat_id)

    async def _group_thread(self) -> int | None:
        """The topic any group send without an incoming message goes to (§10.4)."""
        return await self._topics.answer_topic() if self._topics is not None else None

    async def _send(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to: int | None = None,
        picks: Sequence[tuple[int, str]] = (),
        store: bool = True,
        thread: int | None = None,
    ) -> list[int]:
        """``thread`` is the topic the message belongs to (stored), sent as
        ``message_thread_id`` except for General."""
        try:
            ids = await self._gateway.send_text(
                chat_id,
                text,
                reply_to=reply_to,
                keyboard=decision_keyboard(picks),
                thread_id=send_thread(thread),
            )
        except TelegramBadRequest as e:
            if thread is None or "thread" not in e.message.lower():
                raise
            await self._answer_topic_broken(thread, e.message)
            return []
        if ids and picks:
            await self._decisions.attach_message([d for d, _ in picks], chat_id, ids[-1])
        if not store:
            return ids
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
                thread_id=thread,
            )
        )
        return ids

    async def _answer_topic_broken(self, thread: int, error: str) -> None:
        """§10.4 edge case: the answer topic was deleted or closed. Red tile + DM the admin,
        at most once an hour."""
        log.error("send to answer topic failed", extra={"thread_id": thread, "error": error})
        recent = (
            self._health is not None
            and self._health.answer_topic_error_at is not None
            and (datetime.now(self._tz) - self._health.answer_topic_error_at).total_seconds() < 3600
        )
        if self._health is not None:
            self._health.topic_error(f"thread {thread}: {error}")
        if recent:
            return
        admin = next((u for u in self._users if u.is_admin), None)
        if admin is not None:
            await self._gateway.send_text(
                admin.telegram_id,
                "⚠️ I couldn't post in the group's answer topic (it may have been deleted or "
                "closed). Pick a new one in the dashboard, or send /settopic inside the topic "
                "you want me in.",
            )

    # --- commands ------------------------------------------------------------------------------

    async def _command(
        self,
        cmd: Command,
        msg: Message,
        chat: ChatContext,
        actor: UserRecord,
        thread: int | None = None,
    ) -> None:
        if cmd.name == "start":
            await self._send(chat.chat_id, start_text(self._users), store=False, thread=thread)
        elif cmd.name == "pick":
            await self._cmd_pick(cmd.args, chat, actor, thread)
        elif cmd.name == "options":
            await self._cmd_options(cmd.args, chat, thread)
        elif cmd.name in ("quiet", "unquiet"):
            await self._cmd_quiet(cmd, chat, thread)
        elif cmd.name == "inbox":
            await self._cmd_inbox(chat, actor, thread)
        else:
            await self._send(chat.chat_id, HELP_TEXT, store=False, thread=thread)

    async def _cmd_settopic(self, msg: Message, actor: UserRecord, thread: int | None) -> None:
        if self._topics is None or not actor.is_admin:
            return  # only the admin can move Tykee; anyone else is ignored (§10.4)
        if thread is None:
            await self._send(
                msg.chat.id,
                "This group doesn't use topics, so I answer everywhere.",
                store=False,
            )
            return
        await self._topics.set_answer_topic(thread)
        if self._health is not None:
            self._health.topic_ok()
        await self._send(msg.chat.id, "I'll hang out here now 👋", store=False, thread=thread)

    async def _off_topic(self, chat_id: int, thread: int | None) -> None:
        if self._topics is None or await self._topics.off_topic_mode() != "redirect":
            return
        if not self._topics.may_redirect(chat_id, thread):
            return
        answer = await self._topics.answer_topic()
        where = await self._topics.name_of(chat_id, answer)
        await self._send(chat_id, f"Ask me in **{where}** 👋", store=False, thread=thread)

    async def _cmd_pick(
        self, args: str, chat: ChatContext, actor: UserRecord, thread: int | None = None
    ) -> None:
        if not args:
            await self._send(
                chat.chat_id,
                "Usage: /pick <category>, e.g. /pick dinner",
                store=False,
                thread=thread,
            )
            return
        category = await self._decisions.lookup(args)
        if category is None:
            await self._send(
                chat.chat_id,
                f"I don't have a category called _{args}_ yet. Ask me in words and I'll set it up.",
                store=False,
                thread=thread,
            )
            return
        req = PickRequest(category_id=category.id, for_users=default_for_users(chat, actor))
        result = await self._decisions.pick(category, req, asked_by=actor.id, chat_id=chat.chat_id)
        if not result.picks:
            await self._send(
                chat.chat_id,
                f"No saved options for {category.display_name} yet.",
                store=False,
                thread=thread,
            )
            return
        picks = [(p.decision_id, p.name) for p in result.picks]
        await self._send(
            chat.chat_id, f"🎲 {bold_list([n for _, n in picks])}", picks=picks, thread=thread
        )

    async def _cmd_options(self, args: str, chat: ChatContext, thread: int | None = None) -> None:
        category = await self._decisions.lookup(args) if args else None
        if category is None:
            await self._send(chat.chat_id, "Usage: /options <category>", store=False, thread=thread)
            return
        options = await self._decisions.list_options(category)
        if not options:
            text = f"No saved options for {category.display_name} yet."
        else:
            lines = [f"• {o.name}" + (f" ({', '.join(o.tags)})" if o.tags else "") for o in options]
            text = f"**{category.display_name}** options:\n" + "\n".join(lines)
        await self._send(chat.chat_id, text, store=False, thread=thread)

    def _quiet_text(self, until: datetime) -> str:
        local = until.astimezone(self._tz)
        when = (
            f"{local:%H:%M}"
            if local.date() == datetime.now(self._tz).date()
            else f"{local:%a %H:%M}"
        )
        return f"🤐 OK, I'll stay quiet until {when} unless you mention me. /unquiet to undo."

    async def _cmd_quiet(self, cmd: Command, chat: ChatContext, thread: int | None = None) -> None:
        if not chat.is_group:
            await self._send(
                chat.chat_id,
                "Quiet mode is for the group. Here I only talk when you message me.",
                store=False,
            )
            return
        if cmd.name == "unquiet":
            await self._ambient.unmute(chat.chat_id)
            await self._send(
                chat.chat_id,
                "👋 I'm back. I'll chime in when it helps.",
                store=False,
                thread=thread,
            )
            return
        try:
            duration = parse_duration(cmd.args) if cmd.args else None
        except ValueError:
            await self._send(
                chat.chat_id, "Usage: /quiet [30m | 2h | 1d]", store=False, thread=thread
            )
            return
        until = await self._ambient.mute(chat.chat_id, duration)
        await self._send(chat.chat_id, self._quiet_text(until), store=False, thread=thread)

    # --- memory inbox (§6.7) -------------------------------------------------------------------

    async def _cmd_inbox(
        self, chat: ChatContext, actor: UserRecord, thread: int | None = None
    ) -> None:
        if self._memory is None:
            return
        if not actor.is_admin:
            await self._send(
                chat.chat_id,
                "Only the admin can review the memory inbox.",
                store=False,
                thread=thread,
            )
            return
        items = await self._memory.pending()
        if not items:
            await self._send(chat.chat_id, "📥 Memory inbox is empty.", store=False, thread=thread)
            return
        total = await self._memory.pending_count()
        for item in items:
            text = f"📥 **{item.owner}** → {item.target_path}\n{item.content}"
            if item.reason:
                text += f"\n_why: {item.reason}_"
            await self._gateway.send_text(
                chat.chat_id, text, keyboard=inbox_keyboard(item.id), thread_id=send_thread(thread)
            )
        if total > len(items):
            await self._send(
                chat.chat_id,
                f"…and {total - len(items)} more. Run /inbox again after these.",
                store=False,
                thread=thread,
            )

    async def handle_inbox_callback(self, cb: CallbackQuery, actor: UserRecord) -> None:
        parsed = parse_inbox_callback(cb.data)
        if parsed is None or cb.message is None or self._memory is None:
            await self._gateway.answer_callback(cb.id, "That button has expired.")
            return
        if not actor.is_admin:
            await self._gateway.answer_callback(cb.id, "Only the admin can approve memories.")
            return
        item_id, approve = parsed
        before = await self._memory.get(item_id)
        if before is None or before.status != "pending":
            await self._gateway.answer_callback(cb.id, "Already sorted 👍")
        else:
            await self._memory.decide(item_id, approve=approve, user_id=actor.id)
            await self._gateway.answer_callback(cb.id, "Saved ✅" if approve else "Dropped ❌")
        await self._gateway.set_keyboard(cb.message.chat.id, cb.message.message_id, None)

    # --- unprompted replies (§10.2) ------------------------------------------------------------

    async def respond_unprompted(
        self, chat_id: int, actor: UserRecord, text: str, reason: str
    ) -> int | None:
        """Called by the ambient service after the judge says to speak. Never sends fallback
        text: if Claude couldn't produce a reply, staying silent is better than 'brain offline'."""
        thread = await self._group_thread()
        chat = ChatContext(chat_id, is_group=True, thread=await self._history_thread(chat_id, True))
        reply = await self._orchestrator.respond(chat, actor, text, unprompted_reason=reason)
        if not reply.from_llm and not reply.picks:
            return None
        ids = await self._send(
            chat_id, reply.text, picks=reply.picks, store=reply.from_llm, thread=thread
        )
        return ids[0] if ids else None

    # --- reactions -----------------------------------------------------------------------------

    async def handle_reaction(self, reaction: MessageReactionUpdated) -> None:
        emojis = {r.emoji for r in reaction.new_reaction if isinstance(r, ReactionTypeEmoji)}
        if emojis:
            await self._ambient.on_reaction(reaction.chat.id, reaction.message_id, emojis)

    # --- button callbacks ----------------------------------------------------------------------

    async def _mirror_decision(self, decision_id: int, actor: UserRecord) -> None:
        """§8.4: human-readable log in vault/logs/ (the DB stays authoritative)."""
        if self._memory is None:
            return
        info = await self._decisions.describe(decision_id)
        if info is None:
            return
        category, choice, for_users = info
        now = datetime.now(self._tz)
        await self._memory.log_decision(
            f"- {now:%H:%M} · {category} · **{choice}** · for {for_users}"
            f" · ✅ by {actor.display_name}"
        )

    async def handle_callback(self, cb: CallbackQuery, actor: UserRecord) -> None:
        parsed = parse_callback(cb.data)
        if parsed is None or cb.message is None:
            await self._gateway.answer_callback(cb.id, "That button has expired.")
            return
        decision_id, action = parsed
        chat_id, message_id = cb.message.chat.id, cb.message.message_id
        thread = await self._group_thread() if cb.message.chat.type in GROUP_TYPES else None

        fb = await self._decisions.feedback(decision_id, action, actor.id)
        remaining = await self._decisions.open_on_message(chat_id, message_id)
        await self._gateway.set_keyboard(chat_id, message_id, decision_keyboard(remaining))
        if not fb.applied:
            await self._gateway.answer_callback(cb.id, "Already sorted 👍")
            return
        log.info("feedback", extra={"action": action, "user": actor.slug, "decision": decision_id})

        if action == "accept":
            await self._gateway.answer_callback(cb.id, "Locked in ✅")
            await self._mirror_decision(decision_id, actor)
            await self._send(
                chat_id, f"✅ **{fb.choice_text}** it is.", reply_to=message_id, thread=thread
            )
        elif action == "reject":
            await self._gateway.answer_callback(cb.id, "Noted, I'll suggest that less 👌")
        else:
            result = await self._decisions.reroll(fb, asked_by=actor.id, chat_id=chat_id)
            if result is None or not result.picks:
                await self._gateway.answer_callback(cb.id, "Nothing else left 🤷")
                await self._send(
                    chat_id,
                    "That's everything I've got for this one 🤷",
                    reply_to=message_id,
                    thread=thread,
                )
                return
            await self._gateway.answer_callback(cb.id, "🎲")
            picks = [(p.decision_id, p.name) for p in result.picks]
            await self._send(
                chat_id,
                f"🎲 How about {bold_list([n for _, n in picks])}?",
                reply_to=message_id,
                picks=picks,
                thread=thread,
            )
