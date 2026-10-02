"""usage table: one row per Claude response (§13)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class UsageRow:
    user_id: int | None
    purpose: str
    chat_id: int | None
    import_job_id: int | None
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    created_at: str
    web_search_requests: int = 0
    web_fetch_requests: int = 0


def insert(conn: sqlite3.Connection, row: UsageRow) -> None:
    conn.execute(
        "INSERT INTO usage(user_id, purpose, chat_id, import_job_id, model, input_tokens, "
        "output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, created_at, "
        "web_search_requests, web_fetch_requests) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row.user_id,
            row.purpose,
            row.chat_id,
            row.import_job_id,
            row.model,
            row.input_tokens,
            row.output_tokens,
            row.cache_read_tokens,
            row.cache_write_tokens,
            row.cost_usd,
            row.created_at,
            row.web_search_requests,
            row.web_fetch_requests,
        ),
    )


def cost_since(conn: sqlite3.Connection, since_sql: str, *, include_import: bool = True) -> float:
    """Spend since a time. ``include_import=False`` leaves out the bootstrap import, which only
    counts against the monthly cap (§13)."""
    extra = "" if include_import else " AND import_job_id IS NULL"
    (total,) = conn.execute(
        f"SELECT COALESCE(SUM(cost_usd), 0) FROM usage WHERE created_at >= ?{extra}", (since_sql,)
    ).fetchone()
    return float(total)


def web_searches_since(conn: sqlite3.Connection, since_sql: str) -> int:
    """Web searches billed since a time: the daily search cap (§7.5)."""
    (total,) = conn.execute(
        "SELECT COALESCE(SUM(web_search_requests), 0) FROM usage WHERE created_at >= ?",
        (since_sql,),
    ).fetchone()
    return int(total)
