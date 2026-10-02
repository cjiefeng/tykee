"""Read models for dashboard pages, and validated settings writes. All SQL for the dashboard
lives here so the views stay thin."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.brain import index
from app.db.database import Database
from app.settings import RuntimeSettings, set_value
from app.timeutil import local_day_start, local_month_start, to_sql


class SettingsError(ValueError):
    """Changed settings that wouldn't validate; nothing was written."""


def _all_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        r["key"]: json.loads(r["value_json"])
        for r in conn.execute("SELECT key, value_json FROM settings")
    }


async def save_settings(db: Database, changes: dict[str, Any]) -> None:
    """Write ``changes`` only if the full settings set still validates (§11: changes apply
    instantly, so a typo must never reach the running bot)."""

    def _save(conn: sqlite3.Connection) -> None:
        merged = {**_all_settings(conn), **changes}
        try:
            RuntimeSettings.from_rows(merged)
        except ValidationError as e:
            msgs = "; ".join(
                f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()
            )
            raise SettingsError(msgs) from e
        for key, value in changes.items():
            set_value(conn, key, value)

    await db.write(_save)


async def raw_settings(db: Database) -> list[tuple[str, str, str]]:
    rows = await db.read(
        lambda c: c.execute(
            "SELECT key, value_json, updated_at FROM settings ORDER BY key"
        ).fetchall()
    )
    return [(r[0], r[1], r[2]) for r in rows]


# --- overview --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Spend:
    today: float
    month: float
    calls_today: int
    by_purpose: list[tuple[str, int, float]]


async def spend(db: Database, now: datetime, tz: ZoneInfo) -> Spend:
    day = to_sql(local_day_start(now, tz))
    month = to_sql(local_month_start(now, tz))

    def _q(c: sqlite3.Connection) -> Spend:
        today, calls = c.execute(
            "SELECT COALESCE(SUM(cost_usd), 0), COUNT(*) FROM usage WHERE created_at >= ?", (day,)
        ).fetchone()
        (m,) = c.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM usage WHERE created_at >= ?", (month,)
        ).fetchone()
        by = c.execute(
            "SELECT purpose, COUNT(*), SUM(cost_usd) FROM usage WHERE created_at >= ? "
            "GROUP BY purpose ORDER BY 3 DESC",
            (month,),
        ).fetchall()
        return Spend(float(today), float(m), int(calls), [(r[0], r[1], float(r[2])) for r in by])

    return await db.read(_q)


async def counts(db: Database, now: datetime, tz: ZoneInfo) -> dict[str, Any]:
    day = to_sql(local_day_start(now, tz))

    def _q(c: sqlite3.Connection) -> dict[str, Any]:
        idx = index.counts(c)
        return {
            "messages_today": c.execute(
                "SELECT COUNT(*) FROM messages WHERE created_at >= ? AND role = 'user'", (day,)
            ).fetchone()[0],
            "pending_chunks": c.execute(
                "SELECT COUNT(*) FROM chunks WHERE embed_model = ?", (index.PENDING,)
            ).fetchone()[0],
            "inbox_pending": c.execute(
                "SELECT COUNT(*) FROM memory_inbox WHERE status = 'pending'"
            ).fetchone()[0],
            "last_usage": c.execute("SELECT MAX(created_at) FROM usage").fetchone()[0],
            "last_harvest": c.execute(
                "SELECT created_at, status FROM harvest_runs ORDER BY id DESC LIMIT 1"
            ).fetchone(),
            **idx,
        }

    return await db.read(_q)


def db_size(path: Path) -> int:
    return sum(p.stat().st_size for p in (path, Path(f"{path}-wal")) if p.exists())


async def recent_decisions(db: Database, limit: int = 15) -> list[sqlite3.Row]:
    return await db.read(
        lambda c: c.execute(
            "SELECT d.id, d.choice_text, d.status, d.source, d.for_users, d.created_at, "
            "c.display_name AS category FROM decisions d JOIN categories c "
            "ON c.id = d.category_id ORDER BY d.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    )


# --- categories ------------------------------------------------------------------------------


async def categories(db: Database) -> list[sqlite3.Row]:
    return await db.read(
        lambda c: c.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM decisions d WHERE d.category_id = c.id) AS uses, "
            "(SELECT COUNT(*) FROM options o WHERE o.category_id = c.id AND o.active = 1) AS opts, "
            "(SELECT slug FROM categories m WHERE m.id = c.merged_into) AS merged_slug "
            "FROM categories c ORDER BY c.merged_into IS NOT NULL, c.created_at DESC"
        ).fetchall()
    )


async def category_detail(db: Database, category_id: int) -> dict[str, Any] | None:
    def _q(c: sqlite3.Connection) -> dict[str, Any] | None:
        cat = c.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
        if cat is None:
            return None
        return {
            "cat": cat,
            "aliases": [
                r[0]
                for r in c.execute(
                    "SELECT alias FROM category_aliases WHERE category_id = ? ORDER BY alias",
                    (category_id,),
                )
            ],
            "options": c.execute(
                "SELECT o.*, (SELECT COUNT(*) FROM decisions d WHERE d.option_id = o.id "
                "AND d.status = 'accepted') AS accepted FROM options o "
                "WHERE o.category_id = ? ORDER BY o.active DESC, o.name",
                (category_id,),
            ).fetchall(),
            "prefs": c.execute(
                "SELECT p.option_id, u.display_name, p.multiplier FROM option_prefs p "
                "JOIN users u ON u.id = p.user_id JOIN options o ON o.id = p.option_id "
                "WHERE o.category_id = ? ORDER BY p.option_id, u.id",
                (category_id,),
            ).fetchall(),
            "others": c.execute(
                "SELECT id, slug, display_name FROM categories WHERE id != ? "
                "AND merged_into IS NULL ORDER BY slug",
                (category_id,),
            ).fetchall(),
        }

    return await db.read(_q)


# --- conversations ---------------------------------------------------------------------------


async def chats(db: Database) -> list[sqlite3.Row]:
    return await db.read(
        lambda c: c.execute(
            "SELECT chat_id, COUNT(*) AS n, MAX(created_at) AS last FROM messages "
            "GROUP BY chat_id ORDER BY last DESC"
        ).fetchall()
    )


@dataclass(frozen=True)
class TranscriptLine:
    id: int
    created_at: str
    role: str
    who: str
    thread_id: int | None
    text: str
    tool: str | None = None


async def transcript(
    db: Database, chat_id: int, names: dict[int, str], limit: int = 200
) -> list[TranscriptLine]:
    rows = await db.read(
        lambda c: c.execute(
            "SELECT * FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?", (chat_id, limit)
        ).fetchall()
    )
    out: list[TranscriptLine] = []
    for r in reversed(rows):
        blocks = json.loads(r["content"])
        if r["role"] == "tool":
            use: dict[str, Any] = next((b for b in blocks if b.get("type") == "tool_use"), {})
            res: dict[str, Any] = next((b for b in blocks if b.get("type") == "tool_result"), {})
            args = json.dumps(use.get("input", {}), ensure_ascii=False)
            text = f"{args}\n→ {res.get('content', '')}"
            out.append(
                TranscriptLine(
                    r["id"],
                    r["created_at"],
                    "tool",
                    "tool",
                    r["thread_id"],
                    text,
                    tool=str(use.get("name", "?")),
                )
            )
            continue
        who = "Tykee" if r["role"] == "assistant" else names.get(r["user_id"], "?")
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        out.append(TranscriptLine(r["id"], r["created_at"], r["role"], who, r["thread_id"], text))
    return out


# --- ambient & harvest -----------------------------------------------------------------------


async def ambient_log(db: Database, limit: int = 50) -> list[sqlite3.Row]:
    return await db.read(
        lambda c: c.execute(
            "SELECT * FROM ambient_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    )


async def chat_state(db: Database, chat_id: int | None) -> sqlite3.Row | None:
    if chat_id is None:
        return None
    return await db.read(
        lambda c: c.execute("SELECT * FROM chat_state WHERE chat_id = ?", (chat_id,)).fetchone()
    )


async def harvest_runs(db: Database, limit: int = 30) -> list[sqlite3.Row]:
    return await db.read(
        lambda c: c.execute(
            "SELECT r.*, t.name AS topic FROM harvest_runs r LEFT JOIN forum_topics t "
            "ON t.chat_id = r.chat_id AND t.thread_id = r.thread_id ORDER BY r.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    )


async def harvest_cursors(db: Database) -> list[sqlite3.Row]:
    return await db.read(
        lambda c: c.execute(
            "SELECT h.*, t.name AS topic FROM topic_harvest h LEFT JOIN forum_topics t "
            "ON t.chat_id = h.chat_id AND t.thread_id = h.thread_id ORDER BY h.thread_id"
        ).fetchall()
    )


# --- memory ----------------------------------------------------------------------------------


async def notes(db: Database) -> list[sqlite3.Row]:
    return await db.read(
        lambda c: c.execute(
            "SELECT n.*, (SELECT COUNT(*) FROM chunks k WHERE k.note_id = n.id) AS chunks "
            "FROM notes n ORDER BY n.path"
        ).fetchall()
    )


async def inbox(db: Database, status: str = "pending", limit: int = 100) -> list[sqlite3.Row]:
    return await db.read(
        lambda c: c.execute(
            "SELECT * FROM memory_inbox WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit)
        ).fetchall()
    )
