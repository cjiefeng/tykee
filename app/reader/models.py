"""Plain types shared by the reader, its service and the dashboard. No Telethon here: only
``app/reader/telethon_reader.py`` may import it (a test enforces that)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from app.db.repos.messages import Kind

ChatKind = Literal["user", "group", "topic"]
PeerKind = Literal["user", "bot", "self", "group", "channel"]


class ReaderError(Exception):
    """Base class; the message is safe to show in the dashboard."""


class ReaderNotAllowed(ReaderError):
    """The chat isn't an enabled, consented ``reader_chats`` row (or the reader is off)."""


class ReaderRevoked(ReaderError):
    """Telegram says the session is gone (terminated from a phone, logged out, banned)."""


class ReaderChatNotFound(ReaderError):
    """The chat can't be resolved or read any more (left, deleted, bad reference)."""


class ReaderFloodWait(ReaderError):
    def __init__(self, seconds: int) -> None:
        super().__init__(f"Telegram asked to wait {seconds} s")
        self.seconds = seconds


@dataclass(frozen=True)
class ChatRef:
    """How the reader addresses an allowlisted chat (from its ``reader_chats`` row)."""

    peer_id: int  # marked id: users > 0, basic groups -id, supergroups -100…
    access_hash: int | None
    thread_id: int | None = None  # forum topic


@dataclass(frozen=True)
class ChatInfo:
    """What resolving a reference tells us; metadata only, never message content."""

    peer_id: int
    access_hash: int | None
    kind: PeerKind
    title: str
    members: int | None = None
    forum: bool = False


@dataclass(frozen=True)
class DialogName:
    """One row of "Pick from my chats": name, id and type only."""

    peer_id: int
    name: str
    kind: PeerKind
    forum: bool = False


@dataclass(frozen=True)
class Venue:
    title: str
    address: str | None
    lat: float
    lng: float
    google_place_id: str | None = None


@dataclass(frozen=True)
class ReaderMessage:
    id: int
    date: datetime  # UTC
    sender_id: int | None
    text: str  # caption/text, with media placeholders like "[photo]"
    kind: Kind = "text"
    urls: Sequence[tuple[str, bool]] = field(default_factory=tuple)  # Maps links (url, visible)
    venue: Venue | None = None
    location: bool = False  # a bare location pin (never stored as a place)


class Reader(Protocol):
    """What the rest of the app may ask of the account reader. ``ReadOnlyTelegramReader`` is
    the real one; tests use ``tests/fakes/fake_reader.py``."""

    async def fetch_new(self, chat: ChatRef, min_id: int | None) -> list[ReaderMessage]:
        """Messages with id > ``min_id``, oldest first. ``None`` means "not started": only the
        newest message comes back, as the starting point (it isn't stored)."""
        ...

    async def backfill(
        self, chat: ChatRef, since: datetime, before_id: int | None
    ) -> list[ReaderMessage]:
        """History since ``since`` (and below ``before_id``), oldest first, for the import."""
        ...

    async def list_dialog_names(self, limit: int = 50) -> list[DialogName]: ...

    async def resolve(self, ref: str | int) -> ChatInfo: ...

    async def log_out(self) -> None: ...

    async def close(self) -> None: ...
