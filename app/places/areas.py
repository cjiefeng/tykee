"""Areas gazetteer (§10.6 step 1): where "around Tiong Bahru" or "near home" is, without any
online geocoder. Seeded from ``app/seed/areas.json`` (built by ``scripts/build_areas.py`` from
data.gov.sg planning areas, subzones and MRT/LRT stations, plus a few neighbourhoods).

Matching is code: alias normalisation (§8.1), then rapidfuzz ≥ 90. User areas ("home area =
Bishan") are copies of an existing area's centre and radius, so they're always at
neighbourhood level and never an exact address (§10.5 privacy rule).
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.decisions.text import normalise

SEED = Path(__file__).resolve().parent.parent / "seed" / "areas.json"
FUZZY_MIN = 90
_LEAD = re.compile(r"^(?:somewhere |anywhere |a place )?(?:near|around|at|in|by|nearby) (?:the )?")
_TAIL = re.compile(r" (?:area|side|lah|ah)$")


@dataclass(frozen=True)
class Area:
    id: int
    name: str
    kind: str
    lat: float
    lng: float
    radius_m: int
    source: str


def _area(r: sqlite3.Row) -> Area:
    return Area(r["id"], r["name"], r["kind"], r["lat"], r["lng"], r["radius_m"], r["source"])


def seed_areas(conn: sqlite3.Connection) -> int:
    """Insert seed areas that don't exist yet (by name) and their missing aliases. Returns the
    number of areas inserted. An alias someone re-pointed or a user area's alias is kept."""
    rows = json.loads(SEED.read_text(encoding="utf-8"))
    inserted = 0
    for a in rows:
        cur = conn.execute(
            "INSERT OR IGNORE INTO areas(name, kind, lat, lng, radius_m) VALUES (?, ?, ?, ?, ?)",
            (a["name"], a["kind"], a["lat"], a["lng"], a["radius_m"]),
        )
        inserted += cur.rowcount
        (area_id,) = conn.execute("SELECT id FROM areas WHERE name = ?", (a["name"],)).fetchone()
        conn.executemany(
            "INSERT OR IGNORE INTO area_aliases(alias, area_id) VALUES (?, ?)",
            [(alias, area_id) for alias in a["aliases"]],
        )
    return inserted


def clean_query(text: str) -> str:
    """'near Tiong Bahru area' → 'tiong bahru'."""
    q = normalise(text)
    q = _LEAD.sub("", q)
    return _TAIL.sub("", q).strip()


def match(conn: sqlite3.Connection, text: str) -> Area | None:
    q = clean_query(text)
    if not q:
        return None
    row = conn.execute(
        "SELECT a.* FROM area_aliases x JOIN areas a ON a.id = x.area_id WHERE x.alias = ?",
        (q,),
    ).fetchone()
    if row is not None:
        return _area(row)
    from rapidfuzz import fuzz, process

    aliases = {r["alias"]: r["area_id"] for r in conn.execute("SELECT * FROM area_aliases")}
    best = process.extractOne(q, list(aliases), scorer=fuzz.ratio, score_cutoff=FUZZY_MIN)
    if best is None:
        return None
    return get(conn, aliases[best[0]])


def get(conn: sqlite3.Connection, area_id: int) -> Area | None:
    row = conn.execute("SELECT * FROM areas WHERE id = ?", (area_id,)).fetchone()
    return _area(row) if row else None


def in_text(conn: sqlite3.Connection, text: str) -> Area | None:
    """The area an address or a web result's area text names ('…, Tiong Bahru, Singapore'):
    the whole text, then each comma-separated part. User areas aren't matched here."""
    parts = [text, *text.split(",")]
    for part in parts:
        area = match(conn, part)
        if area is not None and area.source != "user":
            return area
    return None


@dataclass(frozen=True)
class UserArea:
    area: Area
    aliases: list[str]
    like: str  # the gazetteer area whose centre it copies


def user_areas(conn: sqlite3.Connection) -> list[UserArea]:
    rows = conn.execute("SELECT * FROM areas WHERE source = 'user' ORDER BY name").fetchall()
    out: list[UserArea] = []
    for r in rows:
        aliases = [
            a[0]
            for a in conn.execute(
                "SELECT alias FROM area_aliases WHERE area_id = ? ORDER BY alias", (r["id"],)
            )
        ]
        like = conn.execute(
            "SELECT name FROM areas WHERE source = 'seed' AND lat = ? AND lng = ? LIMIT 1",
            (r["lat"], r["lng"]),
        ).fetchone()
        out.append(UserArea(_area(r), aliases, like[0] if like else "?"))
    return out


class AreaError(ValueError):
    pass


def save_user_area(
    conn: sqlite3.Connection, name: str, like: str, aliases: list[str]
) -> tuple[Area, Area]:
    """A named area at neighbourhood level: ``like`` must match a gazetteer area, whose centre
    and radius are copied. Never takes coordinates. Returns (user area, the area copied)."""
    name = " ".join(name.split())
    if not name:
        raise AreaError("give the area a name, e.g. Home")
    base = match(conn, like)
    if base is None or base.source == "user":
        raise AreaError(f"{like!r} isn't a known area; use a neighbourhood, town or MRT station")
    own = normalise(name)
    wanted = list(dict.fromkeys([own, *(normalise(a) for a in aliases if normalise(a))]))
    existing = conn.execute("SELECT * FROM areas WHERE name = ?", (name,)).fetchone()
    if existing is not None and existing["source"] != "user":
        raise AreaError(f"{name!r} is already a built-in area")
    for alias in wanted:
        owner = conn.execute(
            "SELECT a.name, a.source FROM area_aliases x JOIN areas a ON a.id = x.area_id "
            "WHERE x.alias = ?",
            (alias,),
        ).fetchone()
        if owner is not None and owner["name"] != name:
            raise AreaError(f"{alias!r} already means {owner['name']}")
    if existing is None:
        cur = conn.execute(
            "INSERT INTO areas(name, kind, lat, lng, radius_m, source) "
            "VALUES (?, 'user', ?, ?, ?, 'user')",
            (name, base.lat, base.lng, base.radius_m),
        )
        area_id = int(cur.lastrowid or 0)
    else:
        area_id = int(existing["id"])
        conn.execute(
            "UPDATE areas SET lat = ?, lng = ?, radius_m = ? WHERE id = ?",
            (base.lat, base.lng, base.radius_m, area_id),
        )
        conn.execute("DELETE FROM area_aliases WHERE area_id = ?", (area_id,))
    conn.executemany(
        "INSERT INTO area_aliases(alias, area_id) VALUES (?, ?)",
        [(a, area_id) for a in wanted],
    )
    area = get(conn, area_id)
    assert area is not None
    return area, base


def delete_user_area(conn: sqlite3.Connection, area_id: int) -> bool:
    row = conn.execute("SELECT source FROM areas WHERE id = ?", (area_id,)).fetchone()
    if row is None or row["source"] != "user":
        return False
    conn.execute("UPDATE places SET area_id = NULL WHERE area_id = ?", (area_id,))
    conn.execute("DELETE FROM areas WHERE id = ?", (area_id,))
    return True
