"""Category resolution (§8.1) and lookups. All functions take a sqlite3 connection and are run
via ``db.write`` / ``db.read``."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Literal

from app.decisions.text import (
    ALIAS_MAX,
    contains_phrase,
    display_name_for,
    normalise,
    slugify,
)

log = logging.getLogger(__name__)

TAU_MIN, TAU_MAX = 0.5, 365.0
_MAX_MERGE_HOPS = 10


@dataclass(frozen=True)
class Category:
    id: int
    slug: str
    display_name: str
    description: str
    recency_tau_days: float
    default_n: int
    allow_generated: bool


@dataclass(frozen=True)
class CatalogEntry:
    slug: str
    display_name: str
    description: str
    uses: int


@dataclass(frozen=True)
class ResolveResult:
    status: Literal["matched", "created", "choose", "error"]
    category: Category | None = None
    via: str = ""
    catalog: list[CatalogEntry] = field(default_factory=list)
    error: str = ""


def _row_to_category(r: sqlite3.Row) -> Category:
    return Category(
        id=r["id"],
        slug=r["slug"],
        display_name=r["display_name"],
        description=r["description"],
        recency_tau_days=r["recency_tau_days"],
        default_n=r["default_n"],
        allow_generated=bool(r["allow_generated"]),
    )


def get_by_id(conn: sqlite3.Connection, category_id: int) -> Category | None:
    """Fetch a category, following ``merged_into`` to the surviving one."""
    cid: int | None = category_id
    for _ in range(_MAX_MERGE_HOPS):
        r = conn.execute("SELECT * FROM categories WHERE id = ?", (cid,)).fetchone()
        if r is None:
            return None
        if r["merged_into"] is None:
            return _row_to_category(r)
        cid = r["merged_into"]
    log.error("merged_into chain too long", extra={"category_id": category_id})
    return None


def get_by_slug(conn: sqlite3.Connection, slug: str) -> Category | None:
    r = conn.execute("SELECT id FROM categories WHERE slug = ?", (slug,)).fetchone()
    return get_by_id(conn, r["id"]) if r else None


def get_by_alias(conn: sqlite3.Connection, text: str) -> Category | None:
    alias = normalise(text)
    if not alias:
        return None
    r = conn.execute(
        "SELECT category_id FROM category_aliases WHERE alias = ?", (alias,)
    ).fetchone()
    return get_by_id(conn, r["category_id"]) if r else None


def lookup(conn: sqlite3.Connection, name: str) -> Category | None:
    """Alias or slug lookup without creating anything (``/pick``, ``random_pick``)."""
    return get_by_alias(conn, name) or get_by_slug(conn, slugify(name))


def add_alias(conn: sqlite3.Connection, text: str, category_id: int) -> None:
    alias = normalise(text)
    if alias and len(alias) <= ALIAS_MAX:
        conn.execute(
            "INSERT OR IGNORE INTO category_aliases(alias, category_id) VALUES (?, ?)",
            (alias, category_id),
        )


def catalog(conn: sqlite3.Connection) -> list[CatalogEntry]:
    rows = conn.execute(
        "SELECT c.slug, c.display_name, c.description, COUNT(d.id) AS uses "
        "FROM categories c LEFT JOIN decisions d ON d.category_id = c.id "
        "WHERE c.merged_into IS NULL GROUP BY c.id ORDER BY uses DESC, c.slug"
    ).fetchall()
    return [CatalogEntry(r["slug"], r["display_name"], r["description"], r["uses"]) for r in rows]


def _create(
    conn: sqlite3.Connection, slug: str, description: str, tau: float, created_by: str
) -> Category:
    tau = min(max(tau, TAU_MIN), TAU_MAX)
    cur = conn.execute(
        "INSERT INTO categories(slug, display_name, description, recency_tau_days, created_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (slug, display_name_for(slug), description.strip(), tau, created_by),
    )
    assert cur.lastrowid is not None
    log.info("category created", extra={"slug": slug, "tau": tau, "by": created_by})
    created = get_by_id(conn, cur.lastrowid)
    assert created is not None
    return created


def resolve(
    conn: sqlite3.Connection,
    *,
    phrase: str,
    proposed_slug: str,
    description: str,
    proposed_tau_days: float,
    use_existing: str | None = None,
    create_new: bool = False,
    created_by: str = "bot",
) -> ResolveResult:
    def _remember(cat: Category) -> None:
        add_alias(conn, phrase, cat.id)
        add_alias(conn, proposed_slug, cat.id)

    if use_existing:
        cat = get_by_slug(conn, slugify(use_existing))
        if cat is None:
            return ResolveResult("error", error=f"no category with slug {use_existing!r}")
        _remember(cat)
        return ResolveResult("matched", cat, via="chosen")

    cat = get_by_alias(conn, phrase) or get_by_alias(conn, proposed_slug)
    if cat is not None:
        return ResolveResult("matched", cat, via="alias")

    slug = slugify(proposed_slug)
    if not slug:
        return ResolveResult("error", error="proposed_slug must contain letters or digits")
    cat = get_by_slug(conn, slug)
    if cat is not None:
        _remember(cat)
        return ResolveResult("matched", cat, via="slug")

    existing = catalog(conn)
    if existing and not create_new:
        return ResolveResult("choose", catalog=existing)

    cat = _create(conn, slug, description or display_name_for(slug), proposed_tau_days, created_by)
    _remember(cat)
    return ResolveResult("created", cat, via="new")


def match_in_text(conn: sqlite3.Connection, text: str) -> Category | None:
    """Fallback mode (§8.5): the longest alias/slug that appears as a whole word in ``text``."""
    haystack = normalise(text)
    if not haystack:
        return None
    best: tuple[int, int] | None = None  # (length, category_id)
    rows = conn.execute(
        "SELECT alias AS name, category_id AS cid FROM category_aliases "
        "UNION ALL SELECT slug, id FROM categories WHERE merged_into IS NULL"
    ).fetchall()
    for r in rows:
        needle = normalise(r["name"])
        if contains_phrase(haystack, needle) and (best is None or len(needle) > best[0]):
            best = (len(needle), r["cid"])
    return get_by_id(conn, best[1]) if best else None
