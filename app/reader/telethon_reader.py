"""The only module that imports Telethon (§10.7). ``ReadOnlyTelegramReader`` wraps a client
logged in as Jack and exposes reading operations only:

- ``fetch_new`` / ``backfill``: ``iter_messages`` on a chat that is an enabled, consented
  ``reader_chats`` row while ``reader.enabled`` is on, checked against the DB on every call;
- ``list_dialog_names``: names, ids and types of recent chats (for the dashboard picker);
- ``resolve``: metadata of a chat reference (id, type, title, member count), for adding a chat;
- ``log_out`` (the dashboard's Disconnect kill switch) and ``close``.

The client is private, created with ``receive_updates=False`` (no update stream from any chat),
and nothing here sends, edits, deletes, forwards, reacts, marks as read or sets online status.
Tests assert the public surface equals ``ALLOWED_OPERATIONS`` and that no other module imports
Telethon.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from aiogram.types import MessageEntity
from telethon import TelegramClient, errors, functions, types, utils
from telethon.sessions import StringSession

from app.db.database import Database
from app.db.repos.messages import Kind
from app.places import links
from app.reader.models import (
    ChatInfo,
    ChatRef,
    DialogName,
    PeerKind,
    ReaderChatNotFound,
    ReaderError,
    ReaderFloodWait,
    ReaderMessage,
    ReaderNotAllowed,
    ReaderRevoked,
    Venue,
)
from app.settings import get_value

log = logging.getLogger(__name__)

ALLOWED_OPERATIONS = frozenset(
    {"fetch_new", "backfill", "list_dialog_names", "resolve", "log_out", "close"}
)
DEVICE_MODEL = "Tykee reader (read-only)"
APP_VERSION = "tykee-reader 1.0"
FETCH_LIMIT = 1000  # per poll; the next poll continues from the cursor
BACKFILL_LIMIT = 20_000
FLOOD_SLEEP_S = 60  # Telethon sleeps through shorter flood waits itself


def _client(session: str, api_id: int, api_hash: str) -> TelegramClient:
    return TelegramClient(
        StringSession(session),
        api_id,
        api_hash,
        receive_updates=False,
        device_model=DEVICE_MODEL,
        system_version="read-only",
        app_version=APP_VERSION,
        flood_sleep_threshold=FLOOD_SLEEP_S,
    )


async def open_reader(session: str, api_id: int, api_hash: str, db: Database) -> Any:
    """Connect with a saved session. Raises ``ReaderRevoked`` when it's no longer valid."""
    client = _client(session, api_id, api_hash)
    try:
        await client.connect()
        authorized = await client.is_user_authorized()
    except (errors.UnauthorizedError, errors.AuthKeyError) as e:
        await client.disconnect()
        raise ReaderRevoked(type(e).__name__) from e
    if not authorized:
        await client.disconnect()
        raise ReaderRevoked("the saved session is no longer authorised")
    return ReadOnlyTelegramReader(client, db)


async def interactive_login(
    api_id: int,
    api_hash: str,
    phone: Callable[[], str],
    code: Callable[[], str],
    password: Callable[[], str],
) -> str:
    """One-time shell login (``python -m app.reader.login``). Returns the ``StringSession``; the
    2FA password is only passed to Telegram, never stored."""
    client = _client("", api_id, api_hash)
    await client.connect()
    try:
        number = phone()
        await client.send_code_request(number)
        try:
            await client.sign_in(number, code())
        except errors.SessionPasswordNeededError:
            await client.sign_in(password=password())
        me = await client.get_me()
        log.info("reader login", extra={"user_id": getattr(me, "id", None)})
        saved: str = client.session.save()
        return saved
    finally:
        await client.disconnect()


def _input_peer(chat: ChatRef) -> Any:
    real_id, peer_type = utils.resolve_id(chat.peer_id)
    if peer_type is types.PeerUser:
        return types.InputPeerUser(real_id, chat.access_hash or 0)
    if peer_type is types.PeerChat:
        return types.InputPeerChat(real_id)
    return types.InputPeerChannel(real_id, chat.access_hash or 0)


def _maps_urls(body: str, entities: Any) -> list[tuple[str, bool]]:
    """Telethon URL entities → the Bot API shape ``links.urls_in_entities`` already parses."""
    converted: list[MessageEntity] = []
    for e in entities or []:
        if isinstance(e, types.MessageEntityUrl):
            converted.append(MessageEntity(type="url", offset=e.offset, length=e.length))
        elif isinstance(e, types.MessageEntityTextUrl):
            converted.append(
                MessageEntity(type="text_link", offset=e.offset, length=e.length, url=e.url)
            )
    return links.urls_in_entities(body, converted)


def _media(m: Any) -> tuple[str, Kind]:
    if getattr(m, "photo", None) is not None:
        return "[photo]", "photo"
    if getattr(m, "sticker", None) is not None:
        return "[sticker]", "sticker"
    if getattr(m, "voice", None) is not None:
        return "[voice note]", "voice"
    if getattr(m, "video", None) is not None:
        return "[video]", "other"
    if getattr(m, "gif", None) is not None:
        return "[GIF]", "other"
    poll = getattr(m, "poll", None)
    if poll is not None:
        question = getattr(getattr(poll, "poll", None), "question", None)
        text = getattr(question, "text", question)
        return (f"[poll: {text}]" if isinstance(text, str) and text else "[poll]"), "other"
    if getattr(m, "contact", None) is not None:
        return "[contact]", "other"
    if getattr(m, "document", None) is not None:
        return "[file]", "other"
    return "", "text"


def to_reader_message(m: Any) -> ReaderMessage | None:
    """A Telethon message → ``ReaderMessage``; None for service messages and empty bodies."""
    if getattr(m, "action", None) is not None:
        return None
    text = (getattr(m, "message", None) or "").strip()
    venue_media = getattr(m, "venue", None)
    venue = None
    location = False
    kind: Kind
    if venue_media is not None:
        geo = getattr(venue_media, "geo", None)
        provider = getattr(venue_media, "provider", "") or ""
        venue = Venue(
            title=str(getattr(venue_media, "title", "") or ""),
            address=getattr(venue_media, "address", None) or None,
            lat=float(getattr(geo, "lat", 0.0) or 0.0),
            lng=float(getattr(geo, "long", 0.0) or 0.0),
            google_place_id=(getattr(venue_media, "venue_id", None) or None)
            if provider == "gplaces"
            else None,
        )
        media, kind = "[location]", "other"
    elif getattr(m, "geo", None) is not None:
        location = True
        media, kind = "[location]", "other"
    else:
        media, kind = _media(m)
    body = " ".join(p for p in (media, text) if p)
    if not body:
        return None
    if getattr(m, "fwd_from", None) is not None:
        body = f"[fwd] {body}"
    date = getattr(m, "date", None) or datetime.now(UTC)
    return ReaderMessage(
        id=int(m.id),
        date=date.astimezone(UTC),
        sender_id=getattr(m, "sender_id", None),
        text=body,
        kind=kind,
        urls=tuple(_maps_urls(text, getattr(m, "entities", None))),
        venue=venue,
        location=location,
    )


def _placeholder(m: Any) -> ReaderMessage:
    return ReaderMessage(int(m.id), m.date.astimezone(UTC), None, "", "other")


def _peer_kind(entity: Any) -> PeerKind:
    if isinstance(entity, types.User):
        if entity.is_self:
            return "self"
        return "bot" if entity.bot else "user"
    if isinstance(entity, types.Channel):
        return "group" if entity.megagroup else "channel"
    return "group"


def _title(entity: Any) -> str:
    if isinstance(entity, types.User):
        name = " ".join(p for p in (entity.first_name, entity.last_name) if p)
        return name or (entity.username or str(entity.id))
    return str(getattr(entity, "title", "") or entity.id)


async def _guard[T](call: Callable[[], Awaitable[T]]) -> T:
    """Telethon errors → reader errors the service understands."""
    try:
        return await call()
    except errors.FloodWaitError as e:
        raise ReaderFloodWait(int(e.seconds)) from e
    except (errors.UnauthorizedError, errors.AuthKeyError) as e:
        raise ReaderRevoked(type(e).__name__) from e
    except (
        errors.ChannelPrivateError,
        errors.ChatIdInvalidError,
        errors.PeerIdInvalidError,
        errors.UsernameInvalidError,
        errors.UsernameNotOccupiedError,
        errors.ChannelInvalidError,
    ) as e:
        raise ReaderChatNotFound(type(e).__name__) from e
    except errors.RPCError as e:
        raise ReaderError(f"Telegram error: {type(e).__name__}") from e
    except ValueError as e:  # get_entity: "Cannot find any entity corresponding to …"
        raise ReaderChatNotFound(str(e)[:200]) from e


class ReadOnlyTelegramReader:
    """See the module docstring. The public surface is exactly ``ALLOWED_OPERATIONS``."""

    __slots__ = ("__client", "__db")

    def __init__(self, client: Any, db: Database) -> None:
        self.__client = client
        self.__db = db

    async def __require_allowlisted(self, chat: ChatRef) -> None:
        """Every read checks the DB again: no caching, so unticking consent, disabling or
        removing a chat, or the master switch, stops reading on the very next call."""

        def _q(c: sqlite3.Connection) -> bool:
            if get_value(c, "reader.enabled") is not True:
                return False
            row = c.execute(
                "SELECT 1 FROM reader_chats WHERE peer_id = ? AND thread_id IS ? AND enabled = 1 "
                "AND consent_at IS NOT NULL",
                (chat.peer_id, chat.thread_id),
            ).fetchone()
            return row is not None

        if not await self.__db.read(_q):
            raise ReaderNotAllowed("that chat isn't on the reader allowlist with consent")

    async def fetch_new(self, chat: ChatRef, min_id: int | None) -> list[ReaderMessage]:
        await self.__require_allowlisted(chat)
        peer = _input_peer(chat)
        if min_id is None:
            newest = await _guard(
                lambda: self.__client.get_messages(peer, limit=1, reply_to=chat.thread_id)
            )
            return [_placeholder(m) for m in list(newest or [])[:1]]

        async def _all() -> list[ReaderMessage]:
            out: list[ReaderMessage] = []
            async for m in self.__client.iter_messages(
                peer, min_id=min_id, reverse=True, limit=FETCH_LIMIT, reply_to=chat.thread_id
            ):
                # Service messages and empty bodies come back with empty text, so the cursor
                # still moves past them.
                out.append(to_reader_message(m) or _placeholder(m))
            return out

        return await _guard(_all)

    async def backfill(
        self, chat: ChatRef, since: datetime, before_id: int | None
    ) -> list[ReaderMessage]:
        await self.__require_allowlisted(chat)
        peer = _input_peer(chat)

        async def _all() -> list[ReaderMessage]:
            out: list[ReaderMessage] = []
            async for m in self.__client.iter_messages(
                peer,
                offset_date=since,
                reverse=True,
                max_id=(before_id + 1) if before_id else 0,
                limit=BACKFILL_LIMIT,
                reply_to=chat.thread_id,
            ):
                msg = to_reader_message(m)
                if msg is not None:
                    out.append(msg)
            return out

        return await _guard(_all)

    async def list_dialog_names(self, limit: int = 50) -> list[DialogName]:
        async def _all() -> list[DialogName]:
            out: list[DialogName] = []
            async for d in self.__client.iter_dialogs(limit=limit):
                entity = d.entity
                out.append(
                    DialogName(
                        peer_id=int(d.id),
                        name=str(d.name or _title(entity)),
                        kind=_peer_kind(entity),
                        forum=bool(getattr(entity, "forum", False)),
                    )
                )
            return out

        return await _guard(_all)

    async def resolve(self, ref: str | int) -> ChatInfo:
        async def _one() -> ChatInfo:
            entity = await self.__client.get_entity(ref)
            kind = _peer_kind(entity)
            members: int | None = None
            if isinstance(entity, types.Chat):
                members = entity.participants_count
            elif isinstance(entity, types.Channel) and kind == "group":
                full = await self.__client(functions.channels.GetFullChannelRequest(entity))
                members = full.full_chat.participants_count
            return ChatInfo(
                peer_id=int(utils.get_peer_id(entity)),
                access_hash=getattr(entity, "access_hash", None),
                kind=kind,
                title=_title(entity),
                members=members,
                forum=bool(getattr(entity, "forum", False)),
            )

        return await _guard(_one)

    async def log_out(self) -> None:
        """Kill switch: ends this session on Telegram's side (it leaves Settings → Devices)."""
        await _guard(self.__client.log_out)

    async def close(self) -> None:
        await self.__client.disconnect()
