"""Stored messages → Messages API turns (§7.2). Only user text and the assistant's final text
are replayed; in groups, consecutive user messages collapse into one turn with speaker tags."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from zoneinfo import ZoneInfo

from anthropic.types import MessageParam

from app.db.repos.messages import StoredMessage
from app.db.repos.users import UserRecord
from app.timeutil import from_sql


def build_messages(
    rows: Sequence[StoredMessage],
    users_by_id: Mapping[int, UserRecord],
    *,
    is_group: bool,
    summary: str | None = None,
) -> list[MessageParam]:
    turns: list[tuple[str, list[str]]] = []
    for row in rows:
        if row.role not in ("user", "assistant") or not row.text.strip():
            continue
        text = row.text.strip()
        if row.role == "user" and is_group:
            user = users_by_id.get(row.user_id) if row.user_id is not None else None
            text = f"[{user.display_name if user else 'someone'}] {text}"
        if turns and turns[-1][0] == row.role:
            turns[-1][1].append(text)
        else:
            turns.append((row.role, [text]))
    while turns and turns[0][0] != "user":
        turns.pop(0)
    if summary and turns:
        turns[0][1].insert(0, f"(Summary of the earlier conversation: {summary.strip()})")
    out: list[MessageParam] = []
    for role, parts in turns:
        if role == "user":
            out.append({"role": "user", "content": "\n".join(parts)})
        else:
            out.append({"role": "assistant", "content": "\n".join(parts)})
    return out


def format_transcript(
    rows: Sequence[StoredMessage],
    users_by_id: Mapping[int, UserRecord],
    tz: ZoneInfo,
    *,
    bot_name: str = "Tykee",
    marker_before_id: int | None = None,
    marker: str = "--- new ---",
) -> str:
    """Plain transcript for the judge and the summarizer: ``[19:02] Jack: text``."""
    lines: list[str] = []
    for row in rows:
        if marker_before_id is not None and row.id == marker_before_id:
            lines.append(marker)
        if row.role == "assistant":
            who = bot_name
        else:
            user = users_by_id.get(row.user_id) if row.user_id is not None else None
            who = user.display_name if user else "someone"
        when = from_sql(row.created_at).astimezone(tz).strftime("%H:%M")
        lines.append(f"[{when}] {who}: {row.text.strip()}")
    return "\n".join(lines)
