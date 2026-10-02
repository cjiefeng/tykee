from __future__ import annotations

import json
import random
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from aiogram.types import Chat, Message, MessageEntity, User

from app.ambient.judge import Judge
from app.ambient.service import AmbientService
from app.brain.memory import MemoryService
from app.brain.retrieval import Retriever
from app.brain.store import NoteStore
from app.config import parse_allowlist
from app.db.database import Database
from app.db.migrate import apply_migrations
from app.db.repos.users import UserRecord, load_enabled, upsert_allowlist
from app.decisions.service import DecisionService
from app.orchestrator.orchestrator import Orchestrator
from app.orchestrator.summary import Summarizer
from app.settings import SettingsStore, seed_settings
from app.telegram.adapter import TelegramAdapter
from app.telegram.addressing import BotIdentity
from tests.fakes.fake_embedder import FakeEmbedder
from tests.fakes.fake_gateway import FakeGateway
from tests.fakes.fake_llm import FakeLLMClient

JACK_TG = 111
PARTNER_TG = 222
STRANGER_TG = 999
GROUP_ID = -100500
BOT = BotIdentity(id=777, username="TykeeBot")
ALLOWLIST = parse_allowlist(f"{JACK_TG}:jack,{PARTNER_TG}:partner")


NOW = datetime(2026, 10, 2, 11, 0, tzinfo=UTC)  # 19:00 in Asia/Singapore
TZ = ZoneInfo("Asia/Singapore")


@dataclass
class Env:
    db: Database
    users: list[UserRecord]
    settings: SettingsStore
    vault: Path
    closers: list[Callable[[], Awaitable[None]]] = field(default_factory=list)

    @property
    def jack(self) -> UserRecord:
        return self.users[0]

    @property
    def partner(self) -> UserRecord:
        return self.users[1]


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    db = Database(tmp_path / "bot.db")
    await db.open()
    await db.run_raw(apply_migrations)

    def _seed(conn: sqlite3.Connection) -> list[UserRecord]:
        seed_settings(conn)
        upsert_allowlist(conn, ALLOWLIST, "Asia/Singapore")
        return load_enabled(conn, ALLOWLIST)

    users = await db.write(_seed)
    e = Env(db=db, users=users, settings=SettingsStore(db), vault=tmp_path / "vault")
    yield e
    for close in e.closers:
        await close()
    await db.close()


_msg_id = 0


def tg_user(uid: int, name: str = "U", is_bot: bool = False) -> User:
    return User(id=uid, is_bot=is_bot, first_name=name)


def tg_message(
    text: str | None = "hello",
    *,
    from_id: int = JACK_TG,
    chat_id: int = GROUP_ID,
    chat_type: str = "supergroup",
    entities: list[MessageEntity] | None = None,
    reply_to: Message | None = None,
    message_id: int | None = None,
    **extra: object,
) -> Message:
    global _msg_id
    _msg_id += 1
    return Message(
        message_id=message_id if message_id is not None else _msg_id,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type=chat_type),
        from_user=tg_user(from_id),
        text=text,
        entities=entities,
        reply_to_message=reply_to,
        **extra,  # type: ignore[arg-type]
    )


def mention(text: str, handle: str = "@TykeeBot") -> tuple[str, list[MessageEntity]]:
    full = f"{handle} {text}"
    return full, [MessageEntity(type="mention", offset=0, length=len(handle))]


@dataclass
class Clock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now = self.now + timedelta(**kw)


@dataclass
class Stack:
    adapter: TelegramAdapter
    gateway: FakeGateway
    decisions: DecisionService
    orchestrator: Orchestrator
    llm: FakeLLMClient
    clock: Clock
    ambient: AmbientService
    summarizer: Summarizer
    memory: MemoryService
    store: NoteStore
    embedder: FakeEmbedder


def make_stack(env: Env, llm: FakeLLMClient | None = None, seed: int = 7) -> Stack:
    clock = Clock()
    llm = llm or FakeLLMClient()
    gw = FakeGateway()
    embedder = FakeEmbedder()
    store = NoteStore(root=env.vault, db=env.db, embedder=embedder, tz=TZ, clock=clock)
    memory = MemoryService(
        store=store,
        retriever=Retriever(env.db, embedder),
        db=env.db,
        settings=env.settings,
        users=env.users,
        clock=clock,
    )
    decisions = DecisionService(
        db=env.db,
        settings=env.settings,
        users=env.users,
        clock=clock,
        rng=random.Random(seed),
        constraints=memory.avoid_tags,
    )
    summarizer = Summarizer(db=env.db, settings=env.settings, llm=llm, users=env.users, tz=TZ)
    orch = Orchestrator(
        db=env.db,
        settings=env.settings,
        llm=llm,
        decisions=decisions,
        users=env.users,
        tz=TZ,
        summarizer=summarizer,
        memory=memory,
        clock=clock,
    )
    ambient = AmbientService(
        db=env.db,
        settings=env.settings,
        judge=Judge(llm),
        decisions=decisions,
        summarizer=summarizer,
        users=env.users,
        tz=TZ,
        memory=memory,
        clock=clock,
    )
    adapter = TelegramAdapter(
        db=env.db,
        gateway=gw,
        orchestrator=orch,
        decisions=decisions,
        ambient=ambient,
        me=BOT,
        users=env.users,
        tz=TZ,
        memory=memory,
    )
    env.closers += [ambient.close, summarizer.close]
    return Stack(
        adapter, gw, decisions, orch, llm, clock, ambient, summarizer, memory, store, embedder
    )


async def seed_category(
    env: Env, slug: str, options: list[tuple[str, list[str]]] | None = None, tau: float = 3.0
) -> int:
    def _seed(conn: sqlite3.Connection) -> int:
        cur = conn.execute(
            "INSERT INTO categories(slug, display_name, description, recency_tau_days) "
            "VALUES (?, ?, ?, ?)",
            (slug, slug.capitalize(), f"choosing {slug}", tau),
        )
        cid = int(cur.lastrowid or 0)
        conn.execute("INSERT INTO category_aliases(alias, category_id) VALUES (?, ?)", (slug, cid))
        for name, tags in options or []:
            conn.execute(
                "INSERT INTO options(category_id, name, tags_json) VALUES (?, ?, ?)",
                (cid, name, json.dumps(tags)),
            )
        return cid

    return await env.db.write(_seed)
