"""Read-only account reader (§10.7, M10): the read-only wrapper, the session vault, chat
references and guard rails, polling into `messages`, harvesting, retention, kill switch."""

from __future__ import annotations

import ast
import asyncio
import json
import sqlite3
import stat
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telethon import types as tl

from app import inbox_appliers
from app.db.repos import messages as messages_repo
from app.harvest import Harvester
from app.importer.service import ImportService
from app.reader import chats
from app.reader.models import (
    ChatInfo,
    ChatRef,
    ReaderFloodWait,
    ReaderMessage,
    ReaderNotAllowed,
    ReaderRevoked,
)
from app.reader.service import ReaderProblem, ReaderService
from app.reader.session import SessionKeyError, SessionVault
from app.reader.telethon_reader import (
    ALLOWED_OPERATIONS,
    DEVICE_MODEL,
    ReadOnlyTelegramReader,
    _client,
    to_reader_message,
)
from app.settings import set_value
from tests.conftest import GROUP_ID, JACK_TG, NOW, PARTNER_TG, TZ, Env, Stack, make_stack
from tests.fakes.fake_batches import FakeBatches
from tests.fakes.fake_llm import FakeLLMClient
from tests.fakes.fake_reader import FakeReader
from tests.unit.test_harvest import episode, extraction, fact

APP = Path(__file__).resolve().parents[2] / "app"
FERNET_KEY = "ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg="
STRANGER = 555


# --- static guarantees -----------------------------------------------------------------------


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module.split(".")[0])
    return out


def test_only_the_wrapper_imports_telethon() -> None:
    offenders = [
        str(p.relative_to(APP))
        for p in APP.rglob("*.py")
        if "telethon" in _imports(p) and p.name != "telethon_reader.py"
    ]
    assert offenders == []


def test_wrapper_exposes_only_the_allowlisted_operations() -> None:
    public = {n for n in dir(ReadOnlyTelegramReader) if not n.startswith("_")}
    assert public == ALLOWED_OPERATIONS
    banned = ("send", "edit", "delete", "forward", "react", "read_ack", "acknowledge", "status")
    assert not [n for n in public if any(b in n for b in banned)]
    src = (APP / "reader" / "telethon_reader.py").read_text(encoding="utf-8")
    for call in ("send_message", "send_read_acknowledge", "edit_message", "delete_messages"):
        assert call not in src
    assert "UpdateStatusRequest" not in src


def test_client_never_receives_updates_and_names_itself() -> None:
    client = _client("", 12345, "0" * 32)
    assert client._no_updates is True
    assert client._init_request.device_model == DEVICE_MODEL


def _sql_strings(path: Path) -> list[str]:
    out: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            out.append("".join(v.value for v in node.values if isinstance(v, ast.Constant)))
    return out


def test_every_messages_query_by_chat_id_filters_by_source() -> None:
    """§10.7: a reader DM's peer id equals the bot's DM chat_id with that person."""
    bad = []
    for path in APP.rglob("*.py"):
        for sql in _sql_strings(path):
            touches = any(
                k in sql
                for k in ("FROM messages", "messages m ", "messages m\n", "UPDATE messages")
            )
            if touches and "chat_id" in sql and "source" not in sql:
                bad.append(f"{path.relative_to(APP)}: {sql[:80]!r}")
    assert bad == []


# --- wrapper ---------------------------------------------------------------------------------


class RawClient:
    """Stands in for a Telethon client; records what was called."""

    def __init__(self, messages: list[Any]) -> None:
        self.messages = messages
        self.calls: list[str] = []

    async def get_messages(self, peer: Any, limit: int, reply_to: int | None) -> list[Any]:
        self.calls.append("get_messages")
        return list(reversed(self.messages))[:limit]

    async def iter_messages(self, peer: Any, **kw: Any) -> AsyncIterator[Any]:
        self.calls.append("iter_messages")
        for m in self.messages:
            if m.id > kw.get("min_id", 0):
                yield m

    async def disconnect(self) -> None:
        self.calls.append("disconnect")


def raw(i: int, text: str, sender: int = PARTNER_TG, **kw: Any) -> Any:
    base = dict(
        id=i, date=NOW, sender_id=sender, message=text, entities=None, action=None, fwd_from=None
    )
    return SimpleNamespace(**{**base, **kw})


async def add_row(env: Env, peer: int = PARTNER_TG, *, consent: bool = True, **kw: Any) -> int:
    def _ins(c: sqlite3.Connection) -> int:
        cur = c.execute(
            "INSERT INTO reader_chats(peer_id, access_hash, kind, thread_id, label, consent_at, "
            "enabled) VALUES (?, 1, ?, ?, ?, ?, ?)",
            (
                peer,
                kw.get("kind", "user"),
                kw.get("thread_id"),
                kw.get("label", "DM with Partner"),
                "2026-10-01 00:00:00" if consent else None,
                kw.get("enabled", 1),
            ),
        )
        return int(cur.lastrowid or 0)

    return await env.db.write(_ins)


async def reader_on(env: Env, on: bool = True) -> None:
    await env.db.write(lambda c: set_value(c, "reader.enabled", on))


async def test_wrapper_refuses_chats_off_the_allowlist(env: Env) -> None:
    client = RawClient([raw(1, "hi")])
    reader = ReadOnlyTelegramReader(client, env.db)
    dm = ChatRef(PARTNER_TG, 1)
    await reader_on(env)
    with pytest.raises(ReaderNotAllowed):
        await reader.fetch_new(dm, 0)  # not on the list at all
    with pytest.raises(ReaderNotAllowed):
        await reader.backfill(dm, NOW - timedelta(days=1), None)
    row = await add_row(env, consent=False)
    with pytest.raises(ReaderNotAllowed):
        await reader.fetch_new(dm, 0)  # listed but no consent
    await env.db.write(
        lambda c: c.execute("UPDATE reader_chats SET consent_at = 'x' WHERE id = ?", (row,))
    )
    assert [m.text for m in await reader.fetch_new(dm, 0)] == ["hi"]
    with pytest.raises(ReaderNotAllowed):
        await reader.fetch_new(ChatRef(PARTNER_TG, 1, thread_id=4), 0)  # other thread
    await reader_on(env, False)
    with pytest.raises(ReaderNotAllowed):
        await reader.fetch_new(dm, 0)  # master switch, checked per call
    assert client.calls == ["iter_messages"]  # nothing reached Telegram when refused


async def test_wrapper_first_fetch_returns_only_the_newest(env: Env) -> None:
    client = RawClient([raw(1, "a"), raw(2, "b")])
    reader = ReadOnlyTelegramReader(client, env.db)
    await reader_on(env)
    await add_row(env)
    start = await reader.fetch_new(ChatRef(PARTNER_TG, 1), None)
    assert [m.id for m in start] == [2] and client.calls == ["get_messages"]


def test_message_conversion() -> None:
    url = "https://maps.app.goo.gl/abc"
    text = f"eat here {url}"
    m = to_reader_message(
        raw(5, text, entities=[tl.MessageEntityUrl(offset=9, length=len(url))], fwd_from=None)
    )
    assert m is not None and m.text == text and m.urls == ((url, True),) and m.kind == "text"
    assert to_reader_message(raw(6, "", action=object())) is None
    photo = to_reader_message(raw(7, "lunch", photo=object()))
    assert photo is not None and photo.text == "[photo] lunch" and photo.kind == "photo"
    pin = to_reader_message(raw(8, "", geo=object()))
    assert pin is not None and pin.location


# --- session vault & chat references -----------------------------------------------------------


def test_session_vault_roundtrip(tmp_path: Path) -> None:
    vault = SessionVault(tmp_path / "reader.session.enc", FERNET_KEY)
    assert vault.load() is None
    vault.save("1AbCsession")
    assert vault.load() == "1AbCsession"
    assert b"1AbCsession" not in vault.path.read_bytes()
    assert stat.S_IMODE(vault.path.stat().st_mode) == 0o600
    other = SessionVault(vault.path, "Q2hhbmdlZEtleUNoYW5nZWRLZXlDaGFuZ2VkS2V5MDA=")
    with pytest.raises(SessionKeyError):
        other.load()
    with pytest.raises(SessionKeyError):
        SessionVault(vault.path, "not-a-key")
    vault.delete()
    assert not vault.exists()


@pytest.mark.parametrize(
    ("raw_ref", "want"),
    [
        ("@jocelyn_t", "jocelyn_t"),
        ("jocelyn_t", "jocelyn_t"),
        ("222", 222),
        ("-100123", -100123),
        ("https://t.me/jocelyn_t", "jocelyn_t"),
        ("t.me/c/987654/12", -100987654),
    ],
)
def test_parse_ref(raw_ref: str, want: str | int) -> None:
    assert chats.parse_ref(raw_ref) == want


@pytest.mark.parametrize("bad", ["", "t.me/+AbCdEf", "https://t.me/joinchat/xyz", "a b"])
def test_parse_ref_rejects(bad: str) -> None:
    with pytest.raises(chats.ChatRefError):
        chats.parse_ref(bad)


def test_guard_rails() -> None:
    def why(info: ChatInfo, topic: int | None = None) -> str | None:
        return chats.refusal(info, group_id=GROUP_ID, max_members=20, topic=topic)

    assert why(ChatInfo(PARTNER_TG, 1, "user", "Partner")) is None
    assert "Saved Messages" in (why(ChatInfo(JACK_TG, 1, "self", "me")) or "")
    assert "Channels" in (why(ChatInfo(-1001, 1, "channel", "News")) or "")
    assert "own group" in (why(ChatInfo(GROUP_ID, 1, "group", "Us", members=3)) or "")
    assert "members" in (why(ChatInfo(-1002, 1, "group", "Big", members=21)) or "")
    assert "members" in (why(ChatInfo(-1002, 1, "group", "Hidden", members=None)) or "")
    assert why(ChatInfo(-1003, 1, "group", "Friends", members=5)) is None
    assert "no topics" in (why(ChatInfo(-1003, 1, "group", "Friends", members=5), 4) or "")
    assert why(ChatInfo(-1004, 1, "group", "Forum", members=5, forum=True), 4) is None
    assert "bots" in (why(ChatInfo(42, 1, "bot", "SomeBot")) or "")


# --- service -----------------------------------------------------------------------------------


class Rig:
    def __init__(self, env: Env, stack: Stack, fake: FakeReader, tmp: Path) -> None:
        self.env, self.stack, self.fake = env, stack, fake
        self.alerts: list[str] = []
        self.vault = SessionVault(tmp / "reader.session.enc", FERNET_KEY)
        self.vault.save("session")
        self.connects = 0
        self.harvester = Harvester(
            db=env.db,
            settings=env.settings,
            llm=stack.llm,
            memory=stack.memory,
            decisions=stack.decisions,
            topics=stack.topics,
            users=env.users,
            tz=TZ,
            group_id=lambda: GROUP_ID,
            health=stack.health,
            places=stack.places,
            clock=stack.clock,
        )
        self.importer = ImportService(
            db=env.db,
            settings=env.settings,
            llm=stack.llm,
            batches=FakeBatches(),
            store=stack.store,
            users=env.users,
            tz=TZ,
            imports_dir=tmp / "imports",
            clock=stack.clock,
        )

        async def connect(session: str) -> FakeReader:
            assert session == "session"
            self.connects += 1
            return fake

        async def alert(text: str) -> None:
            self.alerts.append(text)

        self.service = ReaderService(
            db=env.db,
            settings=env.settings,
            users=env.users,
            tz=TZ,
            vault=self.vault,
            connect=connect,
            alert=alert,
            group_id=lambda: GROUP_ID,
            harvest=self.harvester.tick_reader,
            places=stack.places,
            importer=self.importer,
            health=stack.health,
            clock=stack.clock,
        )

    async def rows(self) -> list[sqlite3.Row]:
        return await self.env.db.read(
            lambda c: c.execute(
                "SELECT * FROM messages WHERE source = 'account_reader' ORDER BY id"
            ).fetchall()
        )

    async def tick(self, minutes: float = 31) -> Any:
        self.stack.clock.advance(minutes=minutes)
        return await self.service.tick()


def msg(i: int, text: str, sender: int | None = PARTNER_TG, **kw: Any) -> ReaderMessage:
    return ReaderMessage(i, kw.pop("date", NOW), sender, text, **kw)


@pytest.fixture
async def rig(env: Env, tmp_path: Path) -> Rig:
    stack = make_stack(env, FakeLLMClient())
    inbox_appliers.register(stack.memory, stack.decisions)
    fake = FakeReader(
        entities={
            "partner_t": ChatInfo(PARTNER_TG, 99, "user", "Partner"),
            -1003: ChatInfo(-1003, 77, "group", "Makan kakis", members=4),
            GROUP_ID: ChatInfo(GROUP_ID, 1, "group", "Us", members=2),
        }
    )
    await reader_on(env)
    return Rig(env, stack, fake, tmp_path)


async def add_dm(r: Rig, consent: bool = True) -> int:
    info = await r.service.resolve("@partner_t", None)
    return (await r.service.add(info, label="DM with Partner", topic=None, consent=consent)).id


async def test_dm_decision_lands_in_the_decision_log(rig: Rig, env: Env) -> None:
    from tests.conftest import seed_category

    await seed_category(env, "dinner", [("Japanese", ["japanese"])])
    rig.fake.history[PARTNER_TG] = [msg(10, "old stuff")]
    await add_dm(rig)
    assert rig.alerts == ["Tykee reader: 'DM with Partner' added to readable chats"]
    await rig.service.tick()  # first poll: starting point only, nothing stored
    assert await rig.rows() == []
    rig.fake.history[PARTNER_TG] += [
        msg(11, "dinner fri? japanese or thai"),
        msg(12, "japanese lah", JACK_TG),
        msg(13, "ok japanese"),
    ]
    rig.stack.llm._replies.append(
        extraction(
            episodes=[episode("dinner", "Japanese", for_users="jack")],
            facts=[fact("partner", "Partner likes Japanese")],
        )
    )
    await rig.tick(10)
    assert await rig.rows() == []  # interval (30 min) not due yet
    results = await rig.tick(25)
    assert [(r.status, r.stored) for r in results] == [("ok", 3)]
    rows = await rig.rows()
    assert [r["tg_message_id"] for r in rows] == [11, 12, 13]
    assert [r["user_id"] for r in rows] == [env.partner.id, env.jack.id, env.partner.id]
    assert {r["chat_id"] for r in rows} == {PARTNER_TG}
    # Harvested at once (Haiku-tier), DM decisions are for both, kept out of chat sessions.
    req = rig.stack.llm.requests[-1]
    assert req.purpose == "harvest" and "private chat" in req.system[0]["text"]
    d = await env.db.read(lambda c: c.execute("SELECT * FROM decisions").fetchone())
    assert (d["choice_text"], d["source"], d["for_users"], d["chat_id"]) == (
        "Japanese",
        "observed",
        "both",
        None,
    )
    inbox = await env.db.read(lambda c: c.execute("SELECT * FROM memory_inbox").fetchall())
    assert [i["content"] for i in inbox] == ["Partner likes Japanese"]
    assert inbox[0]["source"].startswith("reader:DM with Partner/msg:13")
    ch = (await rig.service.chats())[0]
    assert ch.last_harvest == "done: 1 decision, 1 fact" and ch.fetched_today == 3
    logs = list((env.vault / "logs").rglob("*.md"))
    assert len(logs) == 1 and "**Japanese** · for both · seen in DM with Partner" in (
        logs[0].read_text(encoding="utf-8")
    )


async def test_reader_rows_never_leak_into_the_bot_dm(rig: Rig, env: Env) -> None:
    """The DM's peer id is the bot's chat_id with the partner (§10.7 collision)."""
    from tests.conftest import tg_message

    await rig.stack.adapter.handle_message(
        tg_message("hi bot", from_id=PARTNER_TG, chat_id=PARTNER_TG, chat_type="private"),
        env.partner,
    )
    rig.fake.history[PARTNER_TG] = [msg(1, "start")]
    await add_dm(rig)
    await rig.service.tick()
    rig.fake.history[PARTNER_TG].append(msg(2, "secret dm text"))
    rig.stack.llm._replies.append(extraction())
    await rig.tick()
    history = await env.db.read(lambda c: messages_repo.recent(c, PARTNER_TG, 50))
    assert "secret dm text" not in [m.text for m in history]
    assert "hi bot" in [m.text for m in history]


async def test_consent_and_disable_stop_reading(rig: Rig, env: Env) -> None:
    rig.fake.history[PARTNER_TG] = [msg(1, "a")]
    chat_id = await add_dm(rig, consent=False)
    assert await rig.service.tick() == []  # no consent: never fetched
    assert rig.fake.calls == [("resolve", "partner_t")]
    await rig.service.set_consent(chat_id, True)
    assert rig.alerts[-1] == "Tykee reader: consent recorded for 'DM with Partner'"
    assert len(await rig.service.tick()) == 1
    await rig.service.set_consent(chat_id, False)
    assert await rig.tick() == []
    await rig.service.set_consent(chat_id, True)
    await rig.service.update(chat_id, label="DM", enabled=False, interval_min=30, retention_days=7)
    assert await rig.tick() == []
    actions = [r["action"] for r in reversed(await rig.service.audit_log())]
    assert actions == ["add", "consent", "unconsent", "consent", "disable", "rename"]


async def test_add_refusals(rig: Rig) -> None:
    with pytest.raises(ReaderProblem, match="own group"):
        await rig.service.resolve(str(GROUP_ID), None)
    with pytest.raises(ReaderProblem, match="couldn't find"):
        await rig.service.resolve("@nobody_here", None)
    await add_dm(rig)
    info = await rig.service.resolve("@partner_t", None)
    with pytest.raises(ReaderProblem, match="already"):
        await rig.service.add(info, label="", topic=None, consent=True)


async def test_revoked_session_disables_reader_and_alerts(rig: Rig, env: Env) -> None:
    rig.fake.history[PARTNER_TG] = [msg(1, "a")]
    await add_dm(rig)
    rig.fake.fail = ReaderRevoked("AuthKeyUnregisteredError")
    results = await rig.service.tick()
    assert [r.status for r in results] == ["revoked"]
    s = await env.settings.load()
    assert s.reader_enabled is False and not rig.vault.exists()
    assert rig.stack.health.reader_state == "revoked"
    assert "turned itself off" in rig.alerts[-1]
    assert (await rig.service.chats())[0].status == "revoked"


async def test_flood_wait_pauses_all_polling(rig: Rig) -> None:
    rig.fake.history[PARTNER_TG] = [msg(1, "a")]
    await add_dm(rig)
    rig.fake.fail = ReaderFloodWait(3600)
    assert [r.status for r in await rig.service.tick()] == ["flood_wait"]
    assert await rig.tick(31) == []  # still inside the wait
    assert [r.status for r in await rig.tick(60)] == ["ok"]


async def test_retention_and_remove(rig: Rig, env: Env) -> None:
    rig.fake.history[PARTNER_TG] = [msg(1, "a")]
    chat_id = await add_dm(rig)
    await rig.service.tick()
    rig.fake.history[PARTNER_TG] += [msg(2, "b", date=NOW + timedelta(minutes=20))]
    rig.stack.llm._replies.append(extraction())
    await rig.tick()
    assert len(await rig.rows()) == 1
    rig.stack.clock.advance(days=6)
    assert await rig.service.purge() == 0
    rig.stack.clock.advance(days=2)
    assert await rig.service.purge() == 1  # harvested and older than 7 days
    rig.fake.history[PARTNER_TG] += [msg(3, "c", date=rig.stack.clock.now)]
    await env.db.write(lambda c: set_value(c, "harvest.enabled", False))
    await rig.tick()
    assert len(await rig.rows()) == 1  # unharvested: kept
    assert await rig.service.remove(chat_id) == 1
    assert await rig.rows() == [] and await rig.service.chats() == []


async def test_disconnect_logs_out_and_deletes_session(rig: Rig, env: Env) -> None:
    rig.fake.history[PARTNER_TG] = [msg(1, "a")]
    await add_dm(rig)
    outcome = await rig.service.disconnect()
    assert outcome == "logged out on Telegram" and rig.fake.logged_out
    assert not rig.vault.exists() and not rig.service.logged_in
    assert (await env.settings.load()).reader_enabled is False
    with pytest.raises(ReaderProblem):
        await rig.service.set_enabled(True)


async def test_new_login_is_picked_up_without_restart(rig: Rig) -> None:
    rig.fake.history[PARTNER_TG] = [msg(1, "a")]
    await add_dm(rig)
    await rig.service.tick()
    assert rig.connects == 1
    await asyncio.sleep(0.01)
    rig.vault.save("session")  # the shell login wrote a new file
    import os

    os.utime(rig.vault.path, (1, 1))
    await rig.tick()
    assert rig.connects == 2 and rig.fake.closed == 1


async def test_backfill_feeds_the_import_wizard(rig: Rig, env: Env) -> None:
    rig.fake.history[PARTNER_TG] = [
        msg(1, "long ago", date=NOW - timedelta(days=300)),
        msg(2, "dinner? sushi", date=NOW - timedelta(days=20)),
        msg(3, "ok sushi", JACK_TG, date=NOW - timedelta(days=20)),
        msg(4, "newest", date=NOW),
    ]
    chat_id = await add_dm(rig)
    await rig.service.tick()  # baseline = 4
    rig.fake.history[PARTNER_TG].append(msg(5, "live", date=NOW + timedelta(minutes=5)))
    ch = await rig.service.chat(chat_id)
    job_id = await rig.service.backfill(ch)
    job = await rig.importer.job(job_id)
    assert job.status == "uploaded" and job.msg_count == 3  # 6 months, up to the baseline
    assert job.sender_map == {f"user{PARTNER_TG}": "partner", f"user{JACK_TG}": "jack"}
    export = json.loads(rig.importer.export_path(job).read_text())
    assert [m["text"] for m in export["messages"]] == ["dinner? sushi", "ok sushi", "newest"]
    assert (await rig.service.audit_log())[0]["action"] == "backfill"
