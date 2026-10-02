"""✅ / 🎲 / ❌ feedback (§8.4). Runs inside one ``db.write`` transaction."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Literal

from app.decisions.engine import PickRequest

Action = Literal["accept", "reroll", "reject"]

ACCEPT_FACTOR = 1.05
REJECT_FACTOR = 0.85
PREF_MIN, PREF_MAX = 0.1, 3.0

_STATUS: dict[Action, str] = {"accept": "accepted", "reroll": "rerolled", "reject": "rejected"}


@dataclass(frozen=True)
class FeedbackResult:
    applied: bool  # False → decision missing or no longer 'suggested'
    decision_id: int
    category_id: int = 0
    choice_text: str = ""
    option_id: int | None = None
    request: PickRequest | None = None  # for rerolls


def bump_pref(conn: sqlite3.Connection, option_id: int, user_id: int, factor: float) -> None:
    conn.execute(
        "INSERT INTO option_prefs(option_id, user_id, multiplier) VALUES (?, ?, MIN(?, MAX(?, ?))) "
        "ON CONFLICT(option_id, user_id) DO UPDATE SET "
        "multiplier = MIN(?, MAX(?, multiplier * ?))",
        (option_id, user_id, PREF_MAX, PREF_MIN, factor, PREF_MAX, PREF_MIN, factor),
    )


def _persist_generated(conn: sqlite3.Connection, row: sqlite3.Row) -> int:
    """A generated candidate that gets accepted becomes a real option (§8.1)."""
    tags: list[str] = []
    if row["context_json"]:
        req = PickRequest.from_json(row["context_json"])
        tags = next(
            (
                x.tags
                for x in req.extra_candidates
                if x.name.casefold() == row["choice_text"].casefold()
            ),
            [],
        )
    owner = "shared" if row["for_users"] == "both" else row["for_users"]
    conn.execute(
        "INSERT OR IGNORE INTO options(category_id, name, tags_json, owner, created_by) "
        "VALUES (?, ?, ?, ?, 'bot')",
        (row["category_id"], row["choice_text"], json.dumps(tags, ensure_ascii=False), owner),
    )
    (oid,) = conn.execute(
        "SELECT id FROM options WHERE category_id = ? AND name = ?",
        (row["category_id"], row["choice_text"]),
    ).fetchone()
    conn.execute("UPDATE decisions SET option_id = ? WHERE id = ?", (oid, row["id"]))
    return int(oid)


def apply(
    conn: sqlite3.Connection, decision_id: int, action: Action, user_id: int
) -> FeedbackResult:
    cur = conn.execute(
        "UPDATE decisions SET status = ? WHERE id = ? AND status = 'suggested'",
        (_STATUS[action], decision_id),
    )
    if cur.rowcount == 0:
        return FeedbackResult(applied=False, decision_id=decision_id)
    row = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
    option_id: int | None = row["option_id"]

    if action == "accept":
        if option_id is None:
            option_id = _persist_generated(conn, row)
        bump_pref(conn, option_id, user_id, ACCEPT_FACTOR)
    elif action == "reject" and option_id is not None:
        bump_pref(conn, option_id, user_id, REJECT_FACTOR)

    return FeedbackResult(
        applied=True,
        decision_id=decision_id,
        category_id=row["category_id"],
        choice_text=row["choice_text"],
        option_id=option_id,
        request=PickRequest.from_json(row["context_json"]) if row["context_json"] else None,
    )
