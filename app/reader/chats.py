"""Pure helpers for the reader's chat allowlist (§10.7): parsing what the admin typed into a
reference Telethon can resolve, and the guard rails on what may be added."""

from __future__ import annotations

import re

from app.reader.models import ChatInfo

_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_LINK = re.compile(r"^(?:https?://)?(?:www\.)?(?:t|telegram)\.me/(.+)$", re.IGNORECASE)


class ChatRefError(ValueError):
    """Shown to the admin as is."""


def parse_ref(raw: str) -> str | int:
    """``@username``, ``username``, a numeric id (``-100…`` for supergroups), ``t.me/username``
    or a ``t.me/c/<id>/…`` message link → a username or a marked numeric id. Invite links are
    refused: the reader never joins anything."""
    text = raw.strip()
    if not text:
        raise ChatRefError("enter a @username, a numeric id or a t.me link")
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    m = _LINK.match(text)
    if m:
        path = m.group(1).split("?")[0].strip("/")
        parts = path.split("/")
        if parts[0].startswith("+") or parts[0] == "joinchat":
            raise ChatRefError("invite links aren't supported: the reader never joins chats")
        if parts[0] == "c" and len(parts) > 1 and parts[1].isdigit():
            return int(f"-100{parts[1]}")
        text = parts[0]
    name = text.removeprefix("@")
    if not _USERNAME.match(name):
        raise ChatRefError(f"{raw.strip()!r} doesn't look like a username, id or t.me link")
    return name


def refusal(
    info: ChatInfo, *, group_id: int | None, max_members: int, topic: int | None
) -> str | None:
    """Why this chat can't be added, or None. §10.7 guard rails: no Saved Messages, no
    channels, not the group Tykee's bot already reads, no large groups, no bots."""
    if info.kind == "self":
        return "Saved Messages can't be read."
    if info.kind == "channel":
        return "Channels can't be read, only personal chats and small groups."
    if info.kind == "bot":
        return "Chats with bots can't be read."
    if group_id is not None and info.peer_id == group_id:
        return "That's Tykee's own group; the bot already reads it."
    if info.kind == "group":
        if info.members is None or info.members > max_members:
            count = "an unknown number of" if info.members is None else str(info.members)
            return (
                f"That group has {count} members; only groups of up to {max_members} "
                "(reader.max_group_members) can be read."
            )
        if topic is not None and not info.forum:
            return "That group has no topics; leave the topic empty."
    elif topic is not None:
        return "Only groups with topics can be read per topic."
    return None
