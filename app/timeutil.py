"""Time helpers. Storage is always UTC text in SQLite's ``datetime('now')`` format; day
boundaries use the household timezone (design §5.1)."""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

SQL_FMT = "%Y-%m-%d %H:%M:%S"


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_sql(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(SQL_FMT)


def from_sql(s: str) -> datetime:
    return datetime.strptime(s, SQL_FMT).replace(tzinfo=UTC)


def local_day_start(now: datetime, tz: ZoneInfo) -> datetime:
    """UTC instant of the most recent local midnight in ``tz``."""
    local = now.astimezone(tz)
    return datetime.combine(local.date(), time.min, tzinfo=tz).astimezone(UTC)


def local_month_start(now: datetime, tz: ZoneInfo) -> datetime:
    local = now.astimezone(tz)
    return datetime.combine(local.date().replace(day=1), time.min, tzinfo=tz).astimezone(UTC)
