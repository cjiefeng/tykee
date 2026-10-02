"""Place attributes with provenance (§10.6 step 2): pet-friendliness and friends.

A ``user`` value (one of you said so) always wins and never expires. A ``web`` value is a label
found during discovery: unverified, stale after ``recommend.web_attr_ttl_days``, and never
written to the vault. Conflicting web sources become ``unknown`` with both evidences kept.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit

from app.timeutil import from_sql, to_sql

Source = Literal["user", "web"]

# key → allowed values; the must-have filter passes the values in PASSES.
VALUES: dict[str, tuple[str, ...]] = {
    "pet_friendly": ("yes", "outdoor_only", "no", "unknown"),
    "kid_friendly": ("yes", "no", "unknown"),
    "halal": ("yes", "no", "unknown"),
    "aircon": ("yes", "no", "unknown"),
    "quiet": ("yes", "no", "unknown"),
}
PASSES: dict[str, frozenset[str]] = {
    k: frozenset({"yes", "outdoor_only"} if k == "pet_friendly" else {"yes"}) for k in VALUES
}
LABELS: dict[str, str] = {
    "pet_friendly": "Pet-friendly",
    "kid_friendly": "Kid-friendly",
    "halal": "Halal",
    "aircon": "Aircon",
    "quiet": "Quiet",
}
VALUE_TEXT: dict[str, dict[str, str]] = {
    "pet_friendly": {
        "yes": "pets OK",
        "outdoor_only": "outdoor seating only",
        "no": "no pets",
        "unknown": "couldn't confirm",
    },
}
DEFAULT_TEXT = {"yes": "yes", "no": "no", "unknown": "couldn't confirm"}
EVIDENCE_MAX = 300


class AttrError(ValueError):
    """Unknown key or value."""


@dataclass(frozen=True)
class Attribute:
    place_id: int
    key: str
    value: str
    source: str
    evidence: str | None
    checked_at: str

    def stale(self, now: datetime, ttl_days: int) -> bool:
        return self.source == "web" and now - from_sql(self.checked_at) > timedelta(days=ttl_days)

    def effective(self, now: datetime, ttl_days: int) -> str:
        """What the filter sees: a stale web value counts as unknown."""
        return "unknown" if self.stale(now, ttl_days) else self.value

    def label(self, now: datetime, ttl_days: int) -> str:
        """'outdoor seating only (you confirmed)' / 'pets OK (per sniffy.sg, checked Jun 2026,
        call ahead)'."""
        text = VALUE_TEXT.get(self.key, DEFAULT_TEXT).get(self.value, self.value)
        if self.source == "user":
            return f"{text} (you confirmed)"
        site = _site(self.evidence)
        when = from_sql(self.checked_at).strftime("%b %Y")
        old = ", may be out of date" if self.stale(now, ttl_days) else ""
        per = f"per {site}" if site else "per web"
        return f"{text} ({per}, checked {when}{old}, call ahead)"


def _site(evidence: str | None) -> str:
    for word in (evidence or "").split():
        if word.startswith(("http://", "https://")):
            host = urlsplit(word.rstrip(").,")).hostname or ""
            return host.removeprefix("www.")
    return ""


def _row(r: sqlite3.Row) -> Attribute:
    return Attribute(
        r["place_id"], r["key"], r["value"], r["source"], r["evidence"], r["checked_at"]
    )


def validate(key: str, value: str) -> tuple[str, str]:
    key, value = key.strip().casefold(), value.strip().casefold().replace(" ", "_")
    if key not in VALUES:
        raise AttrError(f"unknown attribute {key!r}; one of {sorted(VALUES)}")
    if value not in VALUES[key]:
        raise AttrError(f"{key} must be one of {list(VALUES[key])}")
    return key, value


def of(conn: sqlite3.Connection, place_ids: Sequence[int]) -> dict[int, dict[str, Attribute]]:
    if not place_ids:
        return {}
    rows = conn.execute(
        f"SELECT * FROM place_attributes WHERE place_id IN ({','.join('?' * len(place_ids))})",
        tuple(place_ids),
    ).fetchall()
    out: dict[int, dict[str, Attribute]] = {}
    for r in rows:
        out.setdefault(r["place_id"], {})[r["key"]] = _row(r)
    return out


def put(
    conn: sqlite3.Connection,
    place_id: int,
    key: str,
    value: str,
    *,
    source: Source,
    evidence: str | None,
    now: datetime,
) -> Attribute:
    """Set an attribute, respecting provenance: user beats web; a web value that disagrees with
    another (unexpired) web value becomes 'unknown' with both evidences."""
    key, value = validate(key, value)
    evidence = (evidence or "").strip()[:EVIDENCE_MAX] or None
    cur = conn.execute(
        "SELECT * FROM place_attributes WHERE place_id = ? AND key = ?", (place_id, key)
    ).fetchone()
    if cur is not None and source == "web":
        old = _row(cur)
        if old.source == "user":
            return old
        if old.value != value and old.value != "unknown" and value != "unknown":
            both = " | ".join(e for e in (old.evidence, evidence) if e)
            value, evidence = "unknown", both[:EVIDENCE_MAX] or None
    conn.execute(
        "INSERT INTO place_attributes(place_id, key, value, source, evidence, checked_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(place_id, key) DO UPDATE SET "
        "value = excluded.value, source = excluded.source, evidence = excluded.evidence, "
        "checked_at = excluded.checked_at",
        (place_id, key, value, source, evidence, to_sql(now)),
    )
    row = conn.execute(
        "SELECT * FROM place_attributes WHERE place_id = ? AND key = ?", (place_id, key)
    ).fetchone()
    return _row(row)


def remove(conn: sqlite3.Connection, place_id: int, key: str) -> bool:
    cur = conn.execute(
        "DELETE FROM place_attributes WHERE place_id = ? AND key = ?", (place_id, key)
    )
    return cur.rowcount > 0


def merge(conn: sqlite3.Connection, src: int, dst: int) -> None:
    """Merging places: the destination keeps its own value unless the source's is user-sourced
    and the destination's isn't."""
    for r in conn.execute("SELECT * FROM place_attributes WHERE place_id = ?", (src,)).fetchall():
        have = conn.execute(
            "SELECT source FROM place_attributes WHERE place_id = ? AND key = ?", (dst, r["key"])
        ).fetchone()
        if have is None or (have["source"] != "user" and r["source"] == "user"):
            conn.execute(
                "INSERT OR REPLACE INTO place_attributes(place_id, key, value, source, evidence, "
                "checked_at) VALUES (?, ?, ?, ?, ?, ?)",
                (dst, r["key"], r["value"], r["source"], r["evidence"], r["checked_at"]),
            )
    conn.execute("DELETE FROM place_attributes WHERE place_id = ?", (src,))


def note_lines(attrs: Sequence[Attribute], day: Callable[[str], str]) -> list[str]:
    """Generated Details bullets for user-confirmed attributes only (web labels stay out of the
    vault, §10.6)."""
    out: list[str] = []
    for a in sorted(attrs, key=lambda a: a.key):
        if a.source != "user":
            continue
        text = VALUE_TEXT.get(a.key, DEFAULT_TEXT).get(a.value, a.value)
        extra = f" ({a.evidence})" if a.evidence else ""
        out.append(f"- {LABELS[a.key]}: {text}{extra}, confirmed {day(a.checked_at)}")
    return out
