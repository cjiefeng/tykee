"""Builders for Telegram Desktop JSON exports (the shapes §15.1 parses)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from tests.conftest import JACK_TG, PARTNER_TG

T0 = datetime(2026, 7, 3, 11, 0, tzinfo=UTC)  # 19:00 in Singapore


def msg(
    i: int,
    text: Any,
    *,
    sender: int = JACK_TG,
    name: str = "Jack",
    at: datetime | None = None,
    **extra: Any,
) -> dict[str, Any]:
    ts = at or T0 + timedelta(minutes=i)
    return {
        "id": i,
        "type": "message",
        "date": ts.astimezone(UTC).replace(tzinfo=None).isoformat(),
        "date_unixtime": str(int(ts.timestamp())),
        "from": name,
        "from_id": f"user{sender}",
        "text": text,
        **extra,
    }


def partner(i: int, text: Any, **kw: Any) -> dict[str, Any]:
    return msg(i, text, sender=PARTNER_TG, name="Sam", **kw)


def single_chat(messages: list[dict[str, Any]], *, chat_id: int = 4242) -> dict[str, Any]:
    return {"name": "Jack & Sam", "type": "personal_chat", "id": chat_id, "messages": messages}


def account(chats: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "about": "Here is the data you requested.",
        "personal_information": {"user_id": JACK_TG, "first_name": "Jack"},
        "chats": {"about": "chats", "list": chats},
        "left_chats": {"about": "left", "list": []},
    }


def dumps(export: dict[str, Any]) -> bytes:
    return json.dumps(export, ensure_ascii=False).encode()


def dinner_chat(start: int = 1, days: int = 4) -> list[dict[str, Any]]:
    """A few evenings of deciding dinner, a day apart."""
    out: list[dict[str, Any]] = [
        {"id": 0, "type": "service", "date": "2026-07-03T18:00:00", "action": "create_group"}
    ]
    i = start
    for d in range(days):
        at = T0 + timedelta(days=d)
        out.append(msg(i, "makan where tonight", at=at))
        out.append(partner(i + 1, "not mala again lah", at=at + timedelta(minutes=2)))
        out.append(msg(i + 2, "ok ytf", at=at + timedelta(minutes=3)))
        i += 3
    return out
