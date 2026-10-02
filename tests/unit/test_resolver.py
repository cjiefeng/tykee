from __future__ import annotations

import sqlite3

from app.decisions import categories as cats
from tests.conftest import Env, seed_category


async def resolve(env: Env, **kw: object) -> cats.ResolveResult:
    args: dict[str, object] = {
        "phrase": "eat tonight",
        "proposed_slug": "dinner",
        "description": "what to eat for dinner",
        "proposed_tau_days": 3,
    }
    args.update(kw)
    return await env.db.write(lambda c: cats.resolve(c, **args))  # type: ignore[arg-type]


async def aliases(env: Env) -> dict[str, int]:
    rows = await env.db.read(lambda c: c.execute("SELECT * FROM category_aliases").fetchall())
    return {r["alias"]: r["category_id"] for r in rows}


async def test_first_category_is_created_with_aliases(env: Env) -> None:
    r = await resolve(env, proposed_tau_days=1000)
    assert r.status == "created" and r.category is not None
    assert r.category.slug == "dinner" and r.category.recency_tau_days == 365.0
    assert await aliases(env) == {"eat tonight": r.category.id, "dinner": r.category.id}


async def test_alias_hit_is_deterministic(env: Env) -> None:
    cid = await seed_category(env, "dinner")
    await env.db.write(lambda c: cats.add_alias(c, "Makan!", cid))
    r = await resolve(env, phrase="makan", proposed_slug="food-tonight")
    assert (r.status, r.via, r.category and r.category.id) == ("matched", "alias", cid)


async def test_exact_slug_hit_adds_alias(env: Env) -> None:
    cid = await seed_category(env, "movie")
    await env.db.write(lambda c: c.execute("DELETE FROM category_aliases"))
    r = await resolve(env, phrase="something to watch", proposed_slug="Movie", description="x")
    assert (r.status, r.via) == ("matched", "slug")
    assert (await aliases(env))["something to watch"] == cid


async def test_unknown_phrase_with_existing_categories_asks_claude(env: Env) -> None:
    await seed_category(env, "dinner")
    await seed_category(env, "movie")
    r = await resolve(env, phrase="board game", proposed_slug="board-game", description="games")
    assert r.status == "choose" and r.category is None
    assert {e.slug for e in r.catalog} == {"dinner", "movie"}
    count = await env.db.read(lambda c: c.execute("SELECT COUNT(*) FROM categories").fetchone())
    assert count[0] == 2  # nothing created yet


async def test_use_existing_records_alias(env: Env) -> None:
    cid = await seed_category(env, "dinner")
    r = await resolve(env, phrase="what to makan", proposed_slug="food", use_existing="dinner")
    assert (r.status, r.via, r.category and r.category.id) == ("matched", "chosen", cid)
    # next time, the same phrasing resolves without Claude choosing
    again = await resolve(env, phrase="What to makan?", proposed_slug="food")
    assert (again.status, again.via) == ("matched", "alias")


async def test_use_existing_unknown_slug_is_error(env: Env) -> None:
    await seed_category(env, "dinner")
    r = await resolve(env, use_existing="nope")
    assert r.status == "error"


async def test_create_new_after_choose(env: Env) -> None:
    await seed_category(env, "dinner")
    r = await resolve(env, phrase="board game", proposed_slug="board-game", create_new=True)
    assert r.status == "created" and r.category is not None
    assert r.category.display_name == "Board game"


async def test_create_new_with_existing_slug_returns_existing(env: Env) -> None:
    cid = await seed_category(env, "dinner")
    r = await resolve(env, phrase="supper", create_new=True)
    assert (r.status, r.category and r.category.id) == ("matched", cid)


async def test_merged_categories_are_followed(env: Env) -> None:
    old = await seed_category(env, "makan")
    new = await seed_category(env, "dinner")

    def _merge(c: sqlite3.Connection) -> None:
        c.execute("UPDATE categories SET merged_into = ? WHERE id = ?", (new, old))

    await env.db.write(_merge)
    r = await resolve(env, phrase="makan", proposed_slug="makan")
    assert r.category is not None and r.category.id == new
    choose = await resolve(env, phrase="x", proposed_slug="something-else")
    assert [e.slug for e in choose.catalog] == ["dinner"]


async def test_fallback_text_match_prefers_longest_alias(env: Env) -> None:
    dinner = await seed_category(env, "dinner")
    weekend = await seed_category(env, "weekend")
    await env.db.write(lambda c: cats.add_alias(c, "weekend dinner plans", weekend))
    hit = await env.db.read(lambda c: cats.match_in_text(c, "any weekend dinner plans?"))
    assert hit is not None and hit.id == weekend
    hit = await env.db.read(lambda c: cats.match_in_text(c, "Dinner?"))
    assert hit is not None and hit.id == dinner
    assert await env.db.read(lambda c: cats.match_in_text(c, "dinnerware")) is None
