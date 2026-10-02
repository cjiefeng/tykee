"""Telegram Desktop JSON exports (§15.1): single-chat ``result.json`` and full-account exports
(``chats.list[]``), parsed as a stream with ijson so a big export never sits in memory.

Pure functions over files; no DB. ``parse()`` yields ``NormalisedMessage``s, so another source
(a WhatsApp ``.txt`` parser, later) only needs to produce the same shape.
"""

from __future__ import annotations

import hashlib
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Literal
from zoneinfo import ZoneInfo

import ijson

from app.places.links import is_maps_link

ExportFormat = Literal["single", "account"]

MAX_JSON_BYTES = 500 * 1024 * 1024  # zip-bomb guard (§15.1)
MAX_ZIP_RATIO = 100
_COPY_CHUNK = 1024 * 1024

_MEDIA = {
    "voice_message": "[voice note]",
    "video_message": "[video message]",
    "video_file": "[video]",
    "animation": "[GIF]",
    "audio_file": "[audio]",
}

# ijson prefixes of the objects we care about, per format.
_SINGLE_MSG = "messages.item"
_ACCOUNT_CHAT = "chats.list.item"
_ACCOUNT_MSG = "chats.list.item.messages.item"


class ExportError(ValueError):
    """The file isn't a usable Telegram export; the message is shown in the dashboard."""


@dataclass(frozen=True)
class NormalisedMessage:
    chat_ref: str
    msg_id: int
    ts: datetime  # UTC
    sender_ref: str  # "user123" ("" when unknown)
    sender_name: str
    text: str


@dataclass
class SenderSummary:
    name: str
    count: int = 0


@dataclass
class ChatSummary:
    ref: str
    name: str
    type: str
    count: int = 0
    first: datetime | None = None
    last: datetime | None = None
    senders: dict[str, SenderSummary] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "name": self.name,
            "type": self.type,
            "count": self.count,
            "first": self.first.isoformat() if self.first else None,
            "last": self.last.isoformat() if self.last else None,
            "senders": {k: [v.name, v.count] for k, v in self.senders.items()},
        }


@dataclass
class ExportSummary:
    format: ExportFormat
    chats: list[ChatSummary]

    @property
    def messages(self) -> int:
        return sum(c.count for c in self.chats)

    def to_json(self) -> dict[str, Any]:
        return {"format": self.format, "chats": [c.to_json() for c in self.chats]}


# --- files -----------------------------------------------------------------------------------


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_COPY_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def is_zip(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(4) == b"PK\x03\x04"


def extract_result_json(zip_path: Path, dest: Path) -> None:
    """Copy ``result.json`` (the shallowest one) out of an export zip and ignore everything
    else. Refuses archives that would expand past the size or ratio limits, and stops copying
    if the stream grows beyond what the header claimed."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        raise ExportError("that zip file is damaged") from e
    with zf:
        members = [
            i for i in zf.infolist() if Path(i.filename).name == "result.json" and not i.is_dir()
        ]
        if not members:
            raise ExportError("no result.json in the zip; export as Machine-readable JSON")
        info = min(members, key=lambda i: i.filename.count("/"))
        if info.file_size > MAX_JSON_BYTES:
            raise ExportError("result.json is larger than 500 MB")
        if info.compress_size and info.file_size / info.compress_size > MAX_ZIP_RATIO:
            raise ExportError("result.json is compressed suspiciously well; refusing to unpack")
        written = 0
        with zf.open(info) as src, dest.open("wb") as out:
            while chunk := src.read(_COPY_CHUNK):
                written += len(chunk)
                if written > MAX_JSON_BYTES:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise ExportError("result.json is larger than 500 MB")
                out.write(chunk)


def copy_limited(src: BinaryIO, dest: Path, limit: int) -> int:
    """Stream ``src`` to ``dest``; raises ExportError past ``limit`` bytes (and removes it)."""
    written = 0
    with dest.open("wb") as out:
        while chunk := src.read(_COPY_CHUNK):
            written += len(chunk)
            if written > limit:
                out.close()
                dest.unlink(missing_ok=True)
                raise ExportError(f"upload is larger than {limit // (1024 * 1024)} MB")
            out.write(chunk)
    return written


# --- parsing ---------------------------------------------------------------------------------


@dataclass
class _Chat:
    ref: str = ""
    name: str = ""
    type: str = ""


def _walk(path: Path) -> Iterator[tuple[ExportFormat, _Chat, dict[str, Any]]]:
    """Yield (format, chat, raw message object) in file order, building only one message at a
    time. Chat metadata (name/type/id) precedes ``messages`` in Telegram's exports."""
    fmt: ExportFormat | None = None
    chat = _Chat()
    builder: Any = None
    msg_prefix = ""
    seen_messages = False
    try:
        with path.open("rb") as f:
            for prefix, event, value in ijson.parse(f, use_float=True):
                if builder is not None:
                    builder.event(event, value)
                    if prefix == msg_prefix and event == "end_map":
                        yield fmt or "single", chat, builder.value
                        builder = None
                    continue
                if event == "map_key" and prefix == "":
                    if value == "chats":
                        fmt = "account"
                    elif value == "messages":
                        fmt, seen_messages = "single", True
                elif prefix == _ACCOUNT_CHAT and event == "start_map":
                    chat = _Chat()
                elif event in ("string", "number") and prefix in ("name", "type", "id"):
                    setattr(chat, "ref" if prefix == "id" else prefix, str(value))
                elif event in ("string", "number") and prefix.startswith(_ACCOUNT_CHAT + "."):
                    key = prefix[len(_ACCOUNT_CHAT) + 1 :]
                    if key in ("name", "type", "id"):
                        setattr(chat, "ref" if key == "id" else key, str(value))
                elif event == "start_map" and prefix in (_SINGLE_MSG, _ACCOUNT_MSG):
                    seen_messages = True
                    builder = ijson.ObjectBuilder()
                    builder.event(event, value)
                    msg_prefix = prefix
    except ijson.JSONError as e:
        raise ExportError(f"not valid JSON ({e})") from e
    if fmt is None and not seen_messages:
        raise ExportError("this JSON isn't a Telegram export (no messages or chats)")


def _part(part: str | dict[str, Any]) -> str:
    if isinstance(part, str):
        return part
    text = str(part.get("text", ""))
    href = str(part.get("href") or "")
    # A Maps link hidden behind link text is kept so it can be resolved (§10.5).
    if part.get("type") == "text_link" and is_maps_link(href) and href not in text:
        return f"{text} ({href})"
    return text


def _flatten(text: Any) -> str:
    if isinstance(text, str):
        return text
    if isinstance(text, list):
        return "".join(_part(part) for part in text if isinstance(part, str | dict))
    return ""


def _timestamp(raw: dict[str, Any], tz: ZoneInfo) -> datetime | None:
    unix = raw.get("date_unixtime")
    if unix is not None:
        try:
            return datetime.fromtimestamp(int(unix), UTC)
        except (TypeError, ValueError):
            pass
    try:
        ts = datetime.fromisoformat(str(raw.get("date", "")))
    except ValueError:
        return None
    # Older exports only have ``date``, in the exporting computer's local time.
    return (ts if ts.tzinfo else ts.replace(tzinfo=tz)).astimezone(UTC)


def _body(raw: dict[str, Any]) -> str:
    text = _flatten(raw.get("text")).strip()
    media = ""
    if "photo" in raw:
        media = "[photo]"
    elif raw.get("media_type") == "sticker":
        media = f"[sticker {raw.get('sticker_emoji', '')}]".replace(" ]", "]")
    elif raw.get("media_type") in _MEDIA:
        media = _MEDIA[str(raw["media_type"])]
    elif "poll" in raw:
        question = raw["poll"].get("question", "") if isinstance(raw["poll"], dict) else ""
        media = f"[poll: {question}]" if question else "[poll]"
    elif "location_information" in raw:
        media = "[location]"
    elif "contact_information" in raw:
        media = "[contact]"
    elif "file" in raw:
        media = "[file]"
    body = " ".join(p for p in (media, text) if p)
    if body and raw.get("forwarded_from"):
        body = f"[fwd] {body}"
    return body


def normalise(chat_ref: str, raw: dict[str, Any], tz: ZoneInfo) -> NormalisedMessage | None:
    if raw.get("type") != "message":
        return None  # service messages: joins, pins, calls…
    ts = _timestamp(raw, tz)
    body = _body(raw)
    if ts is None or not body or not isinstance(raw.get("id"), int | float):
        return None
    return NormalisedMessage(
        chat_ref=chat_ref,
        msg_id=int(raw["id"]),
        ts=ts,
        sender_ref=str(raw.get("from_id") or ""),
        sender_name=str(raw.get("from") or raw.get("actor") or "?"),
        text=body,
    )


def _chat_ref(chat: _Chat) -> str:
    return chat.ref or f"name:{chat.name}"


def parse(path: Path, tz: ZoneInfo, chats: set[str] | None = None) -> Iterator[NormalisedMessage]:
    """Messages of the selected chats (all when ``chats`` is None), in file order."""
    for _fmt, chat, raw in _walk(path):
        ref = _chat_ref(chat)
        if chats is not None and ref not in chats:
            continue
        msg = normalise(ref, raw, tz)
        if msg is not None:
            yield msg


def scan(path: Path, tz: ZoneInfo) -> ExportSummary:
    """Step ② validate: format, chats, counts, date ranges and senders, in one streaming pass."""
    fmt: ExportFormat = "single"
    by_ref: dict[str, ChatSummary] = {}
    for f, chat, raw in _walk(path):
        fmt = f
        ref = _chat_ref(chat)
        summary = by_ref.get(ref)
        if summary is None:
            summary = by_ref[ref] = ChatSummary(ref, chat.name or "(unnamed)", chat.type)
        msg = normalise(ref, raw, tz)
        if msg is None:
            continue
        summary.count += 1
        summary.first = msg.ts if summary.first is None else min(summary.first, msg.ts)
        summary.last = msg.ts if summary.last is None else max(summary.last, msg.ts)
        sender = summary.senders.setdefault(msg.sender_ref, SenderSummary(msg.sender_name))
        sender.count += 1
    chats_with_messages = [c for c in by_ref.values() if c.count]
    if not chats_with_messages:
        raise ExportError("the export has no text messages")
    return ExportSummary(fmt, sorted(chats_with_messages, key=lambda c: -c.count))
