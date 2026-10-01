"""System prompt assembly in cache-friendly order (§7.2):
[1] persona  [2] rules  (cache breakpoint)  [3] pinned notes (cache breakpoint)  [4] dynamic."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from anthropic.types import TextBlockParam

from app.db.repos.users import UserRecord

RULES = """\
Output format (Telegram):
- Plain text only. You may use **bold**, _italic_ and [link text](https://url). Never write HTML, \
markdown headings, tables or code blocks.
- Short messages: usually one to three sentences.

Conversation format:
- In the group chat, each user message is prefixed with the speaker's name in brackets, e.g. \
"[Jack] what should we eat". Several lines in one turn are consecutive messages. Never add these \
prefixes to your own replies.
- You only see a recent window of the conversation. Don't pretend to remember older things.
- When asked to choose between things, commit to one choice with a short reason."""


def build_system(persona: str, dynamic: str, pinned: str | None = None) -> list[TextBlockParam]:
    blocks: list[TextBlockParam] = [
        {"type": "text", "text": persona},
        {"type": "text", "text": RULES, "cache_control": {"type": "ephemeral"}},
    ]
    if pinned:
        blocks.append({"type": "text", "text": pinned, "cache_control": {"type": "ephemeral"}})
    blocks.append({"type": "text", "text": dynamic})
    return blocks


def dynamic_context(
    *,
    now: datetime,
    actor: UserRecord,
    users: Sequence[UserRecord],
    is_group: bool,
) -> str:
    local = now.astimezone(ZoneInfo(actor.timezone))
    lines = [
        f"Current time: {local:%a %Y-%m-%d %H:%M} ({actor.timezone})",
        f"Asking: {actor.display_name}",
    ]
    if is_group:
        names = " and ".join(u.display_name for u in users)
        lines.append(
            f"Chat: the shared group with {names}. Unless the message says otherwise "
            '("just me", "for me"), decisions are for both of them.'
        )
    else:
        lines.append(
            f"Chat: private DM with {actor.display_name}. Unless they say "
            '"we", "us", "both" or "together", decisions are for them only.'
        )
    return "\n".join(lines)
