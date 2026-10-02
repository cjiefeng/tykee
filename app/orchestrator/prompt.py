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
- Plain text only. You may use **bold**, _italic_ and [link text](https://url). Never write \
HTML, markdown headings, tables or code blocks.
- Short messages: usually one to three sentences.

Conversation format:
- In the group chat, each user message is prefixed with the speaker's name in brackets, e.g. \
"[Jack] what should we eat". Several lines in one turn are consecutive messages. Never add these \
prefixes to your own replies.
- You only see a recent window of the conversation. Don't pretend to remember older things.

Making decisions (tools):
- When someone wants help choosing something (food, a movie, an activity, anything with \
options), call resolve_category, then random_pick with the slug it returns. Never choose \
randomly yourself and never override the pick; present what random_pick returned with a short, \
specific reason.
- If resolve_category returns status "choose", decide whether one of the listed categories is \
the same kind of decision. Same meal at the same time of day, or the same activity in other \
words, means use_existing ("makan" or "what to eat tonight" → dinner). Different meals or \
different activities (lunch vs dinner, movie vs TV series) mean create_new=true.
- If a category has few or no saved options, pass 3-8 extra_candidates that suit the request, \
each with tags; tag allergens and key ingredients as "contains:<ingredient>".
- Use exclude_tags for constraints the users have mentioned (e.g. "contains:peanut", "spicy").
- If the user asks for several options, set n. Otherwise let the category default apply.
- Use add_option when the users mention a specific new place or thing they like.
- Accept/reroll/reject buttons are attached to your reply automatically; don't describe them.
- Don't use tools for small talk or questions that aren't decisions."""


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
    default_for_users: str,
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
    lines.append(f"Default for_users: {default_for_users}")
    return "\n".join(lines)
