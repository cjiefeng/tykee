from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    TelegramObject,
    Update,
)

from app.telegram.group import GroupRegistry
from app.telegram.middleware import AccessGate
from tests.conftest import (
    BOT,
    GROUP_ID,
    JACK_TG,
    PARTNER_TG,
    STRANGER_TG,
    Env,
    tg_message,
    tg_user,
)  # fmt: skip
from tests.fakes.fake_gateway import FakeGateway


class Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, event: TelegramObject, data: dict[str, Any]) -> str:
        self.calls.append(data)
        return "handled"


def _gate(env: Env, group_id: int | None = GROUP_ID) -> tuple[AccessGate, FakeGateway]:
    gw = FakeGateway()
    return AccessGate(users=env.users, registry=GroupRegistry(env.db, group_id), gateway=gw), gw


def _msg_update(**kw: Any) -> tuple[Update, dict[str, Any]]:
    msg = tg_message(**kw)
    data = {"event_from_user": msg.from_user, "event_chat": msg.chat}
    return Update(update_id=1, message=msg), data


async def test_allowlisted_group_message_passes_with_actor(env: Env) -> None:
    gate, gw = _gate(env)
    rec = Recorder()
    upd, data = _msg_update(from_id=PARTNER_TG)
    assert await gate(rec, upd, data) == "handled"
    assert rec.calls[0]["actor"].slug == "partner"
    assert gw.left == []


async def test_stranger_in_group_is_dropped(env: Env) -> None:
    gate, gw = _gate(env)
    rec = Recorder()
    upd, data = _msg_update(from_id=STRANGER_TG)
    assert await gate(rec, upd, data) is None
    assert rec.calls == [] and gw.sent == [] and gw.left == []


async def test_stranger_dm_is_dropped(env: Env) -> None:
    gate, _ = _gate(env)
    rec = Recorder()
    upd, data = _msg_update(from_id=STRANGER_TG, chat_id=STRANGER_TG, chat_type="private")
    assert await gate(rec, upd, data) is None
    assert rec.calls == []


async def test_allowlisted_dm_passes(env: Env) -> None:
    gate, _ = _gate(env)
    rec = Recorder()
    upd, data = _msg_update(from_id=JACK_TG, chat_id=JACK_TG, chat_type="private")
    assert await gate(rec, upd, data) == "handled"


async def test_allowlisted_user_in_unknown_group_leaves(env: Env) -> None:
    gate, gw = _gate(env)
    rec = Recorder()
    upd, data = _msg_update(from_id=JACK_TG, chat_id=-999)
    assert await gate(rec, upd, data) is None
    assert rec.calls == [] and gw.left == [-999]


async def test_callback_from_stranger_is_dropped(env: Env) -> None:
    gate, _ = _gate(env)
    rec = Recorder()
    cb = CallbackQuery(id="1", from_user=tg_user(STRANGER_TG), chat_instance="x", data="d:1:a")
    upd = Update(update_id=2, callback_query=cb)
    assert await gate(rec, upd, {"event_from_user": cb.from_user, "event_chat": None}) is None
    assert rec.calls == []


async def test_update_without_user_is_dropped(env: Env) -> None:
    gate, _ = _gate(env)
    rec = Recorder()
    upd, _ = _msg_update()
    assert await gate(rec, upd, {"event_from_user": None, "event_chat": None}) is None


def _added(chat_id: int, by: int) -> Update:
    me = tg_user(BOT.id, is_bot=True)
    return Update(
        update_id=3,
        my_chat_member=ChatMemberUpdated(
            chat=Chat(id=chat_id, type="supergroup"),
            from_user=tg_user(by),
            date=datetime.now(UTC),
            old_chat_member=ChatMemberLeft(user=me),
            new_chat_member=ChatMemberMember(user=me),
        ),
    )


async def test_added_to_unknown_group_by_stranger_leaves_silently(env: Env) -> None:
    gate, gw = _gate(env, group_id=None)
    assert await gate(Recorder(), _added(-42, STRANGER_TG), {}) is None
    assert gw.left == [-42] and gw.sent == []


async def test_admin_adds_bot_without_group_configured_gets_chat_id(env: Env) -> None:
    gate, gw = _gate(env, group_id=None)
    await gate(Recorder(), _added(-42, JACK_TG), {})
    assert gw.left == [-42]
    assert "-42" in gw.sent[0].text


async def test_added_to_configured_group_posts_notice(env: Env) -> None:
    gate, gw = _gate(env)
    await gate(Recorder(), _added(GROUP_ID, PARTNER_TG), {})
    assert gw.left == []
    assert "admin" in gw.sent[0].text


async def test_supergroup_migration_follows_new_id(env: Env) -> None:
    gate, gw = _gate(env)
    rec = Recorder()
    upd, data = _msg_update(from_id=JACK_TG, migrate_to_chat_id=-100777)
    assert await gate(rec, upd, data) is None
    upd2, data2 = _msg_update(from_id=JACK_TG, chat_id=-100777)
    assert await gate(rec, upd2, data2) == "handled"
    assert gw.left == []
