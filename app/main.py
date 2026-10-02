"""Process entrypoint: migrate → seed → run the bot, dashboard and scheduler (harvester, import)."""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path
from zoneinfo import ZoneInfo

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError

from app import inbox_appliers
from app.ambient.judge import Judge
from app.ambient.service import AmbientService
from app.backup import BackupService
from app.brain.embedder import FastEmbedder, cache_dir_for
from app.brain.memory import MemoryService
from app.brain.retrieval import Retriever
from app.brain.store import NoteStore
from app.config import Env
from app.dashboard.app import create_app
from app.dashboard.core import DashboardDeps
from app.dashboard.server import make_server, serve
from app.db.database import Database
from app.db.migrate import apply_migrations
from app.db.repos.users import UserRecord, load_enabled, upsert_allowlist
from app.decisions.service import DecisionService
from app.harvest import Harvester
from app.health import HEARTBEAT_FILE, HealthState, Watchdog, heartbeat
from app.importer.service import ImportService
from app.llm.client import AnthropicLLMClient
from app.logging import LOG_BUFFER, setup_logging
from app.nudges import NudgeService
from app.orchestrator.orchestrator import Orchestrator
from app.orchestrator.summary import Summarizer
from app.places.areas import seed_areas
from app.places.decide import SharedPlaces
from app.places.recommend import RecommendService
from app.places.resolver import PlaceResolver
from app.places.service import PlaceService
from app.reader.models import Reader
from app.reader.service import Connect, ReaderService
from app.reader.session import SessionKeyError, SessionVault
from app.scheduler import Scheduler
from app.settings import SettingsStore, seed_settings
from app.telegram.adapter import TelegramAdapter
from app.telegram.addressing import BotIdentity
from app.telegram.gateway import AiogramGateway
from app.telegram.group import GroupRegistry, resolve_group_id
from app.telegram.middleware import AccessGate
from app.telegram.topics import TopicService, seed_answer_topic

log = logging.getLogger("app")

ALLOWED_UPDATES = [
    "message",
    "edited_message",
    "callback_query",
    "my_chat_member",
    "message_reaction",
]


def check_data_dir(data_dir: Path) -> str | None:
    """Return a human-readable problem if the data dir isn't writable, else None."""
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".write-test"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as e:
        try:
            st = data_dir.stat()
            owner = f"owned by uid {st.st_uid}:{st.st_gid}, mode {stat.filemode(st.st_mode)}"
        except OSError:
            owner = "not accessible"
        return (
            f"cannot write to {data_dir} ({owner}); this process runs as uid {os.getuid()}. "
            f"Fix on the host, e.g. `sudo chown 1000:1000 <host data dir>`. ({e.strerror})"
        )
    return None


async def run(env: Env) -> None:
    db = Database(env.db_path)
    await db.open()
    applied = await db.run_raw(apply_migrations)
    allowlist = env.allowlist

    def _bootstrap(conn: sqlite3.Connection) -> tuple[int | None, list[UserRecord]]:
        seed_answer_topic(conn, env.group_topic_id)  # before seeds: env only fills a missing key
        seed_settings(conn)
        seed_areas(conn)
        upsert_allowlist(conn, allowlist, env.tz)
        return resolve_group_id(conn, env.group_chat_id), load_enabled(conn, allowlist)

    group_id, users = await db.write(_bootstrap)
    log.info(
        "startup",
        extra={
            "migrations_applied": applied,
            "users": len(users),
            "group_configured": group_id is not None,
        },
    )

    bot = Bot(env.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        me_user = await bot.get_me()
        me = BotIdentity(me_user.id, me_user.username or "")
        gateway = AiogramGateway(bot)
        settings = SettingsStore(db)
        health = HealthState()
        llm = AnthropicLLMClient(
            api_key=env.anthropic_api_key,
            db=db,
            settings=settings,
            tz=ZoneInfo(env.tz),
            health=health,
        )
        if not llm.configured:
            log.warning("ANTHROPIC_API_KEY not set: running in fallback mode")
        tz = ZoneInfo(env.tz)
        runtime = await settings.load()
        precision = runtime.embedding_precision
        embedder = FastEmbedder(
            precision, cache_dir_for(precision, env.embed_baked_dir, env.data_dir)
        )
        try:
            await embedder.warm_up()
        except Exception:
            # Notes still get written and keyword search still works; chunks are marked
            # 'pending' and re-embedded on the next reconcile (§14.4).
            log.exception("embedding model failed to load")
            health.embedder_ok = False
        store = NoteStore(root=env.data_dir / "vault", db=db, embedder=embedder, tz=tz)
        created = await store.ensure_skeleton(users)
        stats = await store.reconcile()
        log.info("vault ready", extra={"skeleton_created": created, **stats})
        memory = MemoryService(
            store=store,
            retriever=Retriever(db, embedder),
            db=db,
            settings=settings,
            users=users,
        )
        decisions = DecisionService(
            db=db, settings=settings, users=users, constraints=memory.avoid_tags
        )
        places = PlaceService(
            db=db, settings=settings, resolver=PlaceResolver(db), tz=tz, store=store
        )
        shared_places = SharedPlaces(
            db=db,
            settings=settings,
            places=places,
            decisions=decisions,
            react=gateway.set_reaction,
            tz=tz,
            memory=memory,
        )
        recommend = RecommendService(
            db=db,
            settings=settings,
            places=places,
            users_by_slug={u.slug: u.id for u in users},
            constraints=memory.avoid_tags,
        )
        registry = GroupRegistry(db, group_id)
        topics = TopicService(db=db, settings=settings, group_id=lambda: registry.group_id)
        inbox_appliers.register(memory, decisions, places)
        summarizer = Summarizer(
            db=db,
            settings=settings,
            llm=llm,
            users=users,
            tz=tz,
            history_thread=topics.history_thread,
        )
        orchestrator = Orchestrator(
            db=db,
            settings=settings,
            llm=llm,
            decisions=decisions,
            users=users,
            tz=tz,
            summarizer=summarizer,
            memory=memory,
            places=places,
            recommend=recommend,
            health=health,
        )
        ambient = AmbientService(
            db=db,
            settings=settings,
            judge=Judge(llm),
            decisions=decisions,
            summarizer=summarizer,
            users=users,
            tz=tz,
            memory=memory,
            history_thread=topics.history_thread,
        )
        adapter = TelegramAdapter(
            db=db,
            gateway=gateway,
            orchestrator=orchestrator,
            decisions=decisions,
            ambient=ambient,
            me=me,
            users=users,
            tz=tz,
            memory=memory,
            topics=topics,
            health=health,
            places=shared_places,
            recommend=recommend,
        )
        dp = Dispatcher()
        dp.update.outer_middleware(
            AccessGate(users=users, registry=registry, gateway=gateway, health=health)
        )
        dp.include_router(adapter.router())
        harvester = Harvester(
            db=db,
            settings=settings,
            llm=llm,
            memory=memory,
            decisions=decisions,
            topics=topics,
            users=users,
            tz=tz,
            group_id=lambda: registry.group_id,
            health=health,
            places=places,
        )
        importer = ImportService(
            db=db,
            settings=settings,
            llm=llm,
            batches=llm,
            store=store,
            users=users,
            tz=tz,
            imports_dir=env.data_dir / "imports",
            places=places,
        )
        importer.sweep()
        nudges = NudgeService(
            db=db,
            settings=settings,
            decisions=decisions,
            sender=adapter,
            users=users,
            tz=tz,
            group_id=lambda: registry.group_id,
        )
        backups = BackupService(
            db=db,
            settings=settings,
            backup_dir=env.data_dir / "backups",
            vault=store.root,
            tz=tz,
            health=health,
        )
        reader = make_reader(
            env,
            db=db,
            settings=settings,
            users=users,
            tz=tz,
            gateway=gateway,
            group_id=lambda: registry.group_id,
            harvest=harvester.tick_reader,
            places=places,
            importer=importer,
            health=health,
        )
        scheduler = Scheduler(tz, health)
        scheduler.every_minute("harvest", harvester.tick)
        scheduler.every_minute("import", importer.tick)
        scheduler.every_minute("nudges", nudges.tick)
        scheduler.every_minute("backup", backups.tick)
        scheduler.every_minute("reader", reader.tick)
        scheduler.every("embed_retry", store.retry_pending, minutes=60)
        scheduler.every("place_retry", places.retry_failed, minutes=60)
        scheduler.start()
        beat_task = asyncio.create_task(heartbeat(health, env.data_dir / HEARTBEAT_FILE))
        watchdog = Watchdog(health)
        watchdog.start()
        dashboard: uvicorn.Server | None = None
        dashboard_task: asyncio.Task[None] | None = None
        if env.dashboard_enabled:
            dashboard_app = create_app(
                DashboardDeps(
                    db=db,
                    settings=settings,
                    store=store,
                    memory=memory,
                    decisions=decisions,
                    topics=topics,
                    health=health,
                    gateway=gateway,
                    users=users,
                    tz=tz,
                    group_id=lambda: registry.group_id,
                    db_path=env.db_path,
                    password_hash=env.dashboard_password_hash,
                    session_secret=env.session_secret,
                    log_lines=lambda: list(LOG_BUFFER.lines),
                    harvester=harvester,
                    ambient=ambient,
                    embed_model=embedder.model_id,
                    importer=importer,
                    nudges=nudges,
                    backups=backups,
                    places=places,
                    reader=reader,
                )
            )
            dashboard = make_server(dashboard_app, env.dashboard_host, env.dashboard_port)
            dashboard_task = asyncio.create_task(serve(dashboard))
            log.info("dashboard listening", extra={"port": env.dashboard_port})
        else:
            log.warning(
                "dashboard disabled: set DASHBOARD_PASSWORD_HASH and SESSION_SECRET (32+ chars); "
                "run `python -m app.dashboard.hashpw` to generate them"
            )
        log.info("polling", extra={"bot": me.username})
        try:
            await dp.start_polling(bot, allowed_updates=ALLOWED_UPDATES)
        finally:
            if dashboard is not None and dashboard_task is not None:
                dashboard.should_exit = True
                await dashboard_task
            watchdog.stop()
            beat_task.cancel()
            scheduler.shutdown()
            await ambient.close()
            await summarizer.close()
            await places.resolver.close()
            await reader.close()
            embedder.close()
    finally:
        await bot.session.close()
        await db.close()


def make_reader(
    env: Env,
    *,
    db: Database,
    settings: SettingsStore,
    users: list[UserRecord],
    tz: ZoneInfo,
    gateway: AiogramGateway,
    group_id: Callable[[], int | None],
    harvest: Callable[[], Awaitable[object]],
    places: PlaceService,
    importer: ImportService,
    health: HealthState,
) -> ReaderService:
    """§10.7. Without TG_API_ID/TG_API_HASH/READER_SESSION_KEY the service exists but stays off
    (the dashboard explains what's missing). Telethon is only imported when configured."""
    vault: SessionVault | None = None
    connect: Connect | None = None
    if env.reader_configured and env.tg_api_id is not None:
        try:
            vault = SessionVault(env.reader_session_path, env.reader_session_key)
        except SessionKeyError as e:
            log.error("account reader off", extra={"reason": str(e)})
        else:
            from app.reader.telethon_reader import open_reader

            api_id, api_hash = env.tg_api_id, env.tg_api_hash

            async def _connect(session: str) -> Reader:
                reader: Reader = await open_reader(session, api_id, api_hash, db)
                return reader

            connect = _connect

    admin = next((u for u in users if u.is_admin), None)

    async def alert(text: str) -> None:
        if admin is not None:
            await gateway.send_text(admin.telegram_id, text)

    return ReaderService(
        db=db,
        settings=settings,
        users=users,
        tz=tz,
        vault=vault,
        connect=connect,
        alert=alert,
        group_id=group_id,
        harvest=harvest,
        places=places,
        importer=importer,
        health=health,
    )


def main() -> None:
    env = Env()
    setup_logging(env.log_level)
    problem = check_data_dir(env.data_dir)
    if problem:
        log.error(problem)
        raise SystemExit(1)
    try:
        asyncio.run(run(env))
    except TelegramUnauthorizedError:
        log.error("TELEGRAM_BOT_TOKEN was rejected by Telegram")
        raise SystemExit(1) from None
