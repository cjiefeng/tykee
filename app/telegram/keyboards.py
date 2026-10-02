"""Inline ✅ 🎲 ❌ keyboards for picks (§8.4). Kept free of aiogram types so the gateway port
and tests stay simple."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.decisions.feedback import Action

_CODES: dict[Action, str] = {"accept": "a", "reroll": "r", "reject": "x"}
_ACTIONS: dict[str, Action] = {v: k for k, v in _CODES.items()}
_DATA = re.compile(r"^d:(\d+):([arx])$")
_LABEL_MAX = 24


@dataclass(frozen=True)
class Button:
    text: str
    data: str


Keyboard = list[list[Button]]


def callback_data(decision_id: int, action: Action) -> str:
    return f"d:{decision_id}:{_CODES[action]}"


def parse_callback(data: str | None) -> tuple[int, Action] | None:
    m = _DATA.match(data or "")
    return (int(m.group(1)), _ACTIONS[m.group(2)]) if m else None


def _short(name: str) -> str:
    return name if len(name) <= _LABEL_MAX else name[: _LABEL_MAX - 1] + "…"


def decision_keyboard(picks: Sequence[tuple[int, str]]) -> Keyboard | None:
    """One pick: [✅ Go with it][🎲 Reroll][❌ Not this]. Several: one row per pick."""
    if not picks:
        return None
    if len(picks) == 1:
        did = picks[0][0]
        return [
            [
                Button("✅ Go with it", callback_data(did, "accept")),
                Button("🎲 Reroll", callback_data(did, "reroll")),
                Button("❌ Not this", callback_data(did, "reject")),
            ]
        ]
    return [
        [
            Button(f"✅ {_short(name)}", callback_data(did, "accept")),
            Button("🎲", callback_data(did, "reroll")),
            Button("❌", callback_data(did, "reject")),
        ]
        for did, name in picks
    ]


_INBOX = re.compile(r"^m:(\d+):([ax])$")


def inbox_keyboard(item_id: int) -> Keyboard:
    return [[Button("✅ Save", f"m:{item_id}:a"), Button("❌ Drop", f"m:{item_id}:x")]]


def parse_inbox_callback(data: str | None) -> tuple[int, bool] | None:
    m = _INBOX.match(data or "")
    return (int(m.group(1)), m.group(2) == "a") if m else None
