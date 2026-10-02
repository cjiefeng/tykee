"""Access gate (§10, §12): runs as an outer middleware on every update, before any handler,
DB write or API call. Drops anything not from an allowlisted user, and anything from a group
other than the configured one (leaving that group).

The one thing done for a non-allowlisted actor is *leaving* a group the bot was added to.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Chat, ChatMemberUpdated, TelegramObject, Update, User

from app.db.repos.users import UserRecord
from app.health import HealthState
from app.telegram.formatting import escape
from app.telegram.gateway import ChatGateway
from app.telegram.group import GroupRegistry

log = logging.getLogger(__name__)

GROUP_TYPES = {"group", "supergroup"}
_JOINED = {"member", "administrator"}
_ABSENT = {"left", "kicked"}

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


def join_notice(users: list[UserRecord]) -> str:
    admin = next((u.display_name for u in users if u.is_admin), "the admin")
    return (
        "Hi, I'm <b>Tykee</b> 🎲 I read the messages in this group so I can help when you're "
        "stuck on a decision, but I'll mostly stay quiet. Mention me or reply to me to ask "
        f"something directly.\n\nHeads-up: {escape(admin)} (admin) can see all conversations "
        "and memories in the dashboard."
    )


class AccessGate(BaseMiddleware):
    def __init__(
        self,
        *,
        users: list[UserRecord],
        registry: GroupRegistry,
        gateway: ChatGateway,
        health: HealthState | None = None,
    ) -> None:
        self._health = health
        self._users = users
        self._by_tg = {u.telegram_id: u for u in users}
        self._registry = registry
        self._gateway = gateway

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        assert isinstance(event, Update)
        if self._health is not None:
            self._health.saw_update()
        if event.my_chat_member is not None:
            await self._on_membership(event.my_chat_member)
            return None

        user: User | None = data.get("event_from_user")
        chat: Chat | None = data.get("event_chat")
        actor = self._by_tg.get(user.id) if user is not None else None
        if actor is None:
            log.debug("dropped non-allowlisted update", extra={"update_id": event.update_id})
            return None

        if chat is not None and chat.type == "channel":
            return None
        if chat is not None and chat.type in GROUP_TYPES:
            msg = event.message
            if msg is not None and msg.migrate_to_chat_id is not None:
                await self._registry.migrate(chat.id, msg.migrate_to_chat_id)
                return None
            if msg is not None and msg.migrate_from_chat_id is not None:
                await self._registry.migrate(msg.migrate_from_chat_id, chat.id)
                return None
            if not self._registry.is_allowed(chat.id):
                log.info("message from unknown group; leaving", extra={"chat_id": chat.id})
                await self._gateway.leave_chat(chat.id)
                return None

        data["actor"] = actor
        return await handler(event, data)

    async def _on_membership(self, upd: ChatMemberUpdated) -> None:
        chat = upd.chat
        if chat.type not in GROUP_TYPES:
            return  # DM blocked/unblocked: nothing to do
        joined = upd.new_chat_member.status in _JOINED and upd.old_chat_member.status in _ABSENT
        if not joined:
            return
        if self._registry.is_allowed(chat.id):
            await self._gateway.send_html(chat.id, join_notice(self._users))
            return
        adder = self._by_tg.get(upd.from_user.id)
        if self._registry.group_id is None and adder is not None and adder.is_admin:
            await self._gateway.send_html(
                chat.id,
                f"This group's chat id is <code>{chat.id}</code>. Set "
                "<code>GROUP_CHAT_ID</code> to it in <code>.env</code>, restart me, then add "
                "me again. Leaving for now.",
            )
        log.info("added to a non-configured group; leaving", extra={"chat_id": chat.id})
        await self._gateway.leave_chat(chat.id)
