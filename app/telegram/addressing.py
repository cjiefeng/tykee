"""Pure helpers over incoming messages: is the bot being addressed, what kind of message is
it, and what text do we store for it."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from aiogram.types import Message, MessageEntity

from app.db.repos.messages import Kind

_EMOJI_CATEGORIES = {"So", "Sk", "Mn", "Me", "Cf", "Zs"}
_JOINERS = "\u200d\ufe0f"  # zero-width joiner, emoji variation selector


@dataclass(frozen=True)
class BotIdentity:
    id: int
    username: str  # without '@'


@dataclass(frozen=True)
class Command:
    name: str  # lowercase, without '/'
    args: str


def parse_command(text: str | None, bot_username: str) -> Command | None:
    """``/cmd``, ``/cmd args`` or ``/cmd@ThisBot args``. ``/cmd@OtherBot`` → None."""
    if not text or not text.startswith("/"):
        return None
    head, _, args = text.partition(" ")
    name, _, target = head[1:].partition("@")
    if not name:
        return None
    if target and target.lower() != bot_username.lower():
        return None
    return Command(name.lower(), args.strip())


def _entities(msg: Message) -> list[MessageEntity]:
    return list(msg.entities or msg.caption_entities or [])


def _body(msg: Message) -> str:
    return msg.text or msg.caption or ""


def is_mentioned(msg: Message, me: BotIdentity) -> bool:
    body = _body(msg)
    for e in _entities(msg):
        if e.type == "mention":
            # Offsets are UTF-16 code units (Telegram); extract_from handles that.
            if e.extract_from(body).lower() == f"@{me.username.lower()}":
                return True
        elif e.type == "text_mention" and e.user is not None and e.user.id == me.id:
            return True
    return False


def is_reply_to_bot(msg: Message, me: BotIdentity) -> bool:
    r = msg.reply_to_message
    return r is not None and r.from_user is not None and r.from_user.id == me.id


def is_addressed(msg: Message, me: BotIdentity) -> bool:
    """Stage-1 'always respond' triggers (§10.2): @mention, reply to the bot, or a command."""
    return (
        is_mentioned(msg, me)
        or is_reply_to_bot(msg, me)
        or parse_command(msg.text, me.username) is not None
    )


def _is_emoji_only(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and all(
        unicodedata.category(ch) in _EMOJI_CATEGORIES or ch in _JOINERS for ch in stripped
    )


def classify(msg: Message) -> Kind:
    if msg.sticker is not None:
        return "sticker"
    if msg.photo:
        return "photo"
    if msg.voice is not None or msg.video_note is not None:
        return "voice"
    if msg.text is not None:
        return "emoji" if _is_emoji_only(msg.text) else "text"
    if msg.caption:
        return "text"
    return "other"


def stored_text(msg: Message, kind: Kind) -> str:
    """Text persisted for history replay; non-text messages become placeholders."""
    body = _body(msg)
    placeholder = {
        "sticker": "[sticker]",
        "photo": "[photo]",
        "voice": "[voice]",
        "other": "[media]",
    }
    if msg.venue is not None:
        return f"[venue] {msg.venue.title}"  # address and pin added by PlaceService (§10.5)
    if msg.location is not None:
        return "[location]"  # coordinates are never stored
    if kind in placeholder:
        tag = placeholder[kind]
        if kind == "sticker" and msg.sticker is not None and msg.sticker.emoji:
            tag = f"[sticker {msg.sticker.emoji}]"
        return f"{tag} {body}".strip()
    return body
