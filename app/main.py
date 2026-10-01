"""Process entrypoint: migrate → seed → run the bot (dashboard and scheduler join in M4/M6)."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError

from app.config import Env
from app.db.database import Database
from app.db.migrate import apply_migrations
from app.db.repos.users import UserRecord, load_enabled, upsert_allowlist
from app.llm.client import AnthropicLLMClient
from app.logging import setup_logging
from app.orchestrator.orchestrator import Orchestrator
from app.settings import SettingsStore, seed_settings
from app.telegram.adapter import TelegramAdapter
from app.telegram.addressing import BotIdentity
from app.telegram.gateway import AiogramGateway
from app.telegram.group import GroupRegistry, resolve_group_id
from app.telegram.middleware import AccessGate

log = logging.getLogger("app")

ALLOWED_UPDATES = [
    "message",
    "edited_message",
    "callback_query",
    "my_chat_member",
    "message_reaction",
]


async def run(env: Env) -> None:
    db = Database(env.db_path)
    await db.open()
    applied = await db.run_raw(apply_migrations)
    allowlist = env.allowlist

    def _bootstrap(conn: sqlite3.Connection) -> tuple[int | None, list[UserRecord]]:
        seed_settings(conn)
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
        llm = AnthropicLLMClient(
            api_key=env.anthropic_api_key, db=db, settings=settings, tz=ZoneInfo(env.tz)
        )
        if not llm.configured:
            log.warning("ANTHROPIC_API_KEY not set: running in fallback mode")
        orchestrator = Orchestrator(db=db, settings=settings, llm=llm, users=users)
        adapter = TelegramAdapter(
            db=db, gateway=gateway, orchestrator=orchestrator, me=me, users=users
        )
        dp = Dispatcher()
        dp.update.outer_middleware(
            AccessGate(users=users, registry=GroupRegistry(db, group_id), gateway=gateway)
        )
        dp.include_router(adapter.router())
        log.info("polling", extra={"bot": me.username})
        await dp.start_polling(bot, allowed_updates=ALLOWED_UPDATES)
    finally:
        await bot.session.close()
        await db.close()


def main() -> None:
    env = Env()
    setup_logging(env.log_level)
    try:
        asyncio.run(run(env))
    except TelegramUnauthorizedError:
        log.error("TELEGRAM_BOT_TOKEN was rejected by Telegram")
        raise SystemExit(1) from None
