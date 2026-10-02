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
    place_id: int | None = None
    recommendation: bool = False  # a find_places pick (§10.6): ✅ visits the place


def bump_pref(conn: sqlite3.Connection, option_id: int, user_id: int, factor: float) -> None:
    conn.execute(
        "INSERT INTO option_prefs(option_id, user_id, multiplier) VALUES (?, ?, MIN(?, MAX(?, ?))) "
        "ON CONFLICT(option_id, user_id) DO UPDATE SET "
        "multiplier = MIN(?, MAX(?, multiplier * ?))",
        (option_id, user_id, PREF_MAX, PREF_MIN, factor, PREF_MAX, PREF_MIN, factor),
    )


def place_option(c: sqlite3.Connection, category_id: int, name: str, place_id: int) -> int:
    """The category's option for a place (§10.5): by place, else by name (linking it), else a
    new option tagged ``place``."""
    row = c.execute(
        "SELECT id FROM options WHERE category_id = ? AND place_id = ?", (category_id, place_id)
    ).fetchone()
    if row is not None:
        return int(row[0])
    by_name = c.execute(
        "SELECT id FROM options WHERE category_id = ? AND lower(name) = lower(?)",
        (category_id, name),
    ).fetchone()
    if by_name is not None:
        c.execute(
            "UPDATE options SET place_id = COALESCE(place_id, ?) WHERE id = ?",
            (place_id, by_name[0]),
        )
        return int(by_name[0])
    cur = c.execute(
        "INSERT INTO options(category_id, name, tags_json, created_by, place_id) "
        "VALUES (?, ?, ?, 'bot', ?)",
        (category_id, name, json.dumps(["place"]), place_id),
    )
    return int(cur.lastrowid or 0)


def _pick_request(raw: str | None) -> PickRequest | None:
    """``context_json`` of a random_pick; None for other kinds (a recommendation)."""
    if not raw:
        return None
    try:
        return PickRequest.from_json(raw)
    except (TypeError, KeyError, ValueError):
        return None


def _persist_generated(conn: sqlite3.Connection, row: sqlite3.Row) -> int:
    """A generated candidate that gets accepted becomes a real option (§8.1); a place becomes
    the category's option for that place."""
    if row["place_id"] is not None:
        oid = place_option(conn, row["category_id"], row["choice_text"], row["place_id"])
        conn.execute("UPDATE decisions SET option_id = ? WHERE id = ?", (oid, row["id"]))
        return oid
    tags: list[str] = []
    req = _pick_request(row["context_json"])
    if req is not None:
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
        request=_pick_request(row["context_json"]),
        place_id=row["place_id"],
        recommendation='"recommend"' in (row["context_json"] or ""),
    )
