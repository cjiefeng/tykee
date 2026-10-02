from __future__ import annotations

import json
import random
import sqlite3
from collections import Counter
from datetime import datetime, timedelta

import pytest

from app.decisions import categories as cats
from app.decisions import engine
from app.decisions.engine import ExtraCandidate, PickRequest, recency_factor, sample, tags_ok
from app.timeutil import to_sql
from tests.conftest import NOW, Env, seed_category

# --- pure math (§8.3) ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("days", "expected"),
    [(None, 1.0), (0, 0.0), (1, 0.2835), (3, 0.6321), (7, 0.9030), (30, 1.0)],
)
def test_recency_factor_tau3(days: float | None, expected: float) -> None:
    assert recency_factor(days, 3.0) == pytest.approx(expected, abs=1e-4)


def test_recency_factor_clamps_negative_age() -> None:
    assert recency_factor(-1, 3.0) == 0.0


def test_sample_is_without_replacement() -> None:
    rng = random.Random(1)
    for _ in range(200):
        got = sample([1, 1, 1, 1], 3, rng)
        assert len(got) == 3 == len(set(got))
    assert sorted(sample([1, 2], 5, rng)) == [0, 1]  # n larger than pool


def test_sample_never_picks_zero_weight_when_others_positive() -> None:
    rng = random.Random(2)
    assert all(sample([0, 1, 0], 1, rng) == [1] for _ in range(200))


def test_sample_uniform_when_all_zero() -> None:
    rng = random.Random(3)
    counts = Counter(sample([0, 0, 0], 1, rng)[0] for _ in range(3000))
    assert set(counts) == {0, 1, 2} and min(counts.values()) > 800


def test_sample_follows_weights() -> None:
    rng = random.Random(4)
    counts = Counter(sample([3, 1], 1, rng)[0] for _ in range(8000))
    assert counts[0] / 8000 == pytest.approx(0.75, abs=0.02)


def test_tags_ok() -> None:
    assert tags_ok(["Spicy", "cheap"], ["spicy"], [])
    assert not tags_ok(["spicy"], ["spicy", "cheap"], [])  # include = all
    assert not tags_ok(["contains:peanut"], [], ["contains:peanut", "x"])  # exclude = any


# --- DB-backed pick (§8.2) -------------------------------------------------------------------


async def _pick(
    env: Env, slug: str, rng_seed: int = 0, chat_id: int = -1, **kw: object
) -> engine.PickResult:
    users = {u.slug: u.id for u in env.users}

    def _run(c: sqlite3.Connection) -> engine.PickResult:
        cat = cats.get_by_slug(c, slug)
        assert cat is not None
        req = PickRequest(category_id=cat.id, **{"for_users": "both", **kw})  # type: ignore[arg-type]
        return engine.pick(
            c, cat, req, users_by_slug=users, asked_by=env.jack.id, chat_id=chat_id,
            now=NOW, session_hours=6, rng=random.Random(rng_seed),
        )  # fmt: skip

    return await env.db.write(_run)


async def _set_owner(env: Env, name: str, owner: str) -> None:
    await env.db.write(
        lambda c: c.execute("UPDATE options SET owner=? WHERE name=?", (owner, name))
    )


async def _decision(
    env: Env, cid: int, name: str, status: str, age: timedelta, chat_id: int = -1
) -> None:
    def _ins(c: sqlite3.Connection) -> None:
        oid = c.execute("SELECT id FROM options WHERE name = ?", (name,)).fetchone()
        c.execute(
            "INSERT INTO decisions(category_id, option_id, choice_text, for_users, asked_by, "
            "status, chat_id, created_at) VALUES (?, ?, ?, 'both', ?, ?, ?, ?)",
            (cid, oid[0] if oid else None, name, env.jack.id, status, chat_id, to_sql(NOW - age)),
        )

    await env.db.write(_ins)


async def test_owner_scope(env: Env) -> None:
    await seed_category(env, "dinner", [("Pho", []), ("Jack's curry", []), ("P's salad", [])])
    await _set_owner(env, "Jack's curry", "jack")
    await _set_owner(env, "P's salad", "partner")
    assert set((await _pick(env, "dinner", for_users="jack")).weights) == {"o:1", "o:2"}
    assert len((await _pick(env, "dinner", for_users="both")).weights) == 3


async def test_tag_filters_and_inactive_options(env: Env) -> None:
    await seed_category(
        env, "dinner",
        [("Satay", ["contains:peanut", "grill"]), ("Laksa", ["spicy"]), ("Soup", ["light"])],
    )  # fmt: skip
    await env.db.write(lambda c: c.execute("UPDATE options SET active=0 WHERE name='Soup'"))
    r = await _pick(env, "dinner", exclude_tags=["contains:peanut"])
    assert [p.name for p in r.picks] == ["Laksa"] and r.considered == 1


async def test_extra_candidates_dedupe_and_respect_filters(env: Env) -> None:
    await seed_category(env, "dinner", [("Pho", []), ("Old place", [])])
    await env.db.write(lambda c: c.execute("UPDATE options SET active=0 WHERE name='Old place'"))
    extras = [
        ExtraCandidate("pho", []),  # duplicate of an option
        ExtraCandidate("OLD PLACE", []),  # deactivated option must not sneak back
        ExtraCandidate("Ramen", ["noodles"]),
        ExtraCandidate("Pad thai", ["contains:peanut"]),
    ]
    r = await _pick(env, "dinner", extra_candidates=extras, exclude_tags=["contains:peanut"])
    assert set(r.weights) == {"o:1", "g:ramen"}


async def test_extra_candidates_ignored_when_generation_disallowed(env: Env) -> None:
    await seed_category(env, "dinner", [("Pho", [])])
    await env.db.write(lambda c: c.execute("UPDATE categories SET allow_generated = 0"))
    r = await _pick(env, "dinner", extra_candidates=[ExtraCandidate("Ramen", [])])
    assert set(r.weights) == {"o:1"}


async def test_recency_and_prefs_shape_weights(env: Env) -> None:
    cid = await seed_category(env, "dinner", [("Pho", []), ("Laksa", []), ("Ramen", [])])
    await _decision(env, cid, "Pho", "accepted", timedelta(days=1))
    await _decision(env, cid, "Laksa", "accepted", timedelta(days=7))
    await _decision(env, cid, "Laksa", "accepted", timedelta(days=20))  # older one ignored

    def _prefs(c: sqlite3.Connection) -> None:
        c.execute("INSERT INTO option_prefs VALUES (3, ?, 2.0)", (env.jack.id,))
        c.execute("INSERT INTO option_prefs VALUES (3, ?, 0.5)", (env.partner.id,))
        c.execute("UPDATE options SET base_weight = 2.0 WHERE name = 'Laksa'")

    await env.db.write(_prefs)
    w = (await _pick(env, "dinner")).weights
    assert w["o:1"] == pytest.approx(0.2835, abs=1e-4)
    assert w["o:2"] == pytest.approx(2.0 * 0.9030, abs=1e-4)
    assert w["o:3"] == pytest.approx(1.0)  # 2.0 * 0.5 for 'both'
    w_jack = (await _pick(env, "dinner", for_users="jack")).weights
    assert w_jack["o:3"] == pytest.approx(2.0)


async def test_recency_applies_to_generated_candidates_by_name(env: Env) -> None:
    cid = await seed_category(env, "dinner")
    await _decision(env, cid, "Ramen", "accepted", timedelta(days=3))
    r = await _pick(env, "dinner", extra_candidates=[ExtraCandidate("ramen", [])])
    assert r.weights["g:ramen"] == pytest.approx(0.6321, abs=1e-4)


async def test_session_exclusion(env: Env) -> None:
    cid = await seed_category(env, "dinner", [("Pho", []), ("Laksa", []), ("Ramen", [])])
    await _decision(env, cid, "Pho", "rejected", timedelta(hours=1))
    await _decision(env, cid, "Laksa", "rerolled", timedelta(hours=7))  # outside 6h window
    await _decision(env, cid, "Ramen", "rejected", timedelta(hours=1), chat_id=-2)  # other chat
    assert set((await _pick(env, "dinner")).weights) == {"o:2", "o:3"}


async def test_pick_inserts_suggested_decisions_with_context(env: Env) -> None:
    await seed_category(env, "dinner", [("Pho", []), ("Laksa", []), ("Ramen", [])])
    r = await _pick(env, "dinner", n=2, include_tags=[])
    assert len(r.picks) == 2 and len({p.name for p in r.picks}) == 2
    rows = await env.db.read(lambda c: c.execute("SELECT * FROM decisions").fetchall())
    assert [row["status"] for row in rows] == ["suggested", "suggested"]
    ctx = json.loads(rows[0]["context_json"])
    assert ctx["for_users"] == "both" and ctx["n"] == 2


async def test_n_is_capped(env: Env) -> None:
    await seed_category(env, "dinner", [(f"o{i}", []) for i in range(10)])
    assert len((await _pick(env, "dinner", n=50)).picks) == engine.MAX_N


async def test_empty_category_returns_no_picks(env: Env) -> None:
    await seed_category(env, "dinner")
    r = await _pick(env, "dinner")
    assert r.picks == [] and r.considered == 0


async def test_non_repeating_over_a_week(env: Env) -> None:
    """Accepting every pick for 7 nights with τ=3 and 7 options: simulation gives a back-to-back
    repeat rate of ~5.2% vs ~14.3% for uniform picks. 150 seeded runs (900 transitions) put the
    bound of 8% ~4 sd above expected and ~8 sd below uniform."""
    from app.decisions import feedback

    repeats = 0
    trials = 150
    for seed in range(trials):
        await env.db.write(lambda c: c.execute("DELETE FROM decisions"))
        await env.db.write(lambda c: c.execute("DELETE FROM option_prefs"))
        await env.db.write(lambda c: c.execute("DELETE FROM options"))
        await env.db.write(lambda c: c.execute("DELETE FROM category_aliases"))
        await env.db.write(lambda c: c.execute("DELETE FROM categories"))
        await seed_category(env, "dinner", [(f"dish{i}", []) for i in range(7)])
        rng = random.Random(seed)
        last = None
        for night in range(7):
            now = NOW + timedelta(days=night)

            def _night(c: sqlite3.Connection, now: datetime = now, rng: random.Random = rng) -> str:
                cat = cats.get_by_slug(c, "dinner")
                assert cat is not None
                r = engine.pick(
                    c, cat, PickRequest(cat.id, "both"), users_by_slug={"jack": env.jack.id},
                    asked_by=env.jack.id, chat_id=-1, now=now, session_hours=6, rng=rng,
                )  # fmt: skip
                feedback.apply(c, r.picks[0].decision_id, "accept", env.jack.id)
                return r.picks[0].name

            name = await env.db.write(_night)
            repeats += name == last
            last = name
    assert repeats / (trials * 6) < 0.08
