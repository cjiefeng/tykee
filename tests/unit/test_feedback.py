from __future__ import annotations

import sqlite3

import pytest

from app.decisions import feedback
from app.decisions.engine import ExtraCandidate, PickRequest
from app.timeutil import to_sql
from tests.conftest import NOW, Env, seed_category


async def _suggest(
    env: Env, cid: int, name: str, option_id: int | None, ctx: PickRequest | None = None
) -> int:
    def _ins(c: sqlite3.Connection) -> int:
        cur = c.execute(
            "INSERT INTO decisions(category_id, option_id, choice_text, for_users, asked_by, "
            "status, chat_id, context_json, created_at) VALUES (?, ?, ?, 'both', ?, 'suggested', "
            "-1, ?, ?)",
            (cid, option_id, name, env.jack.id, ctx.to_json() if ctx else None, to_sql(NOW)),
        )
        return int(cur.lastrowid or 0)

    return await env.db.write(_ins)


async def _pref(env: Env, option_id: int, user_id: int) -> float | None:
    row = await env.db.read(
        lambda c: c.execute(
            "SELECT multiplier FROM option_prefs WHERE option_id=? AND user_id=?",
            (option_id, user_id),
        ).fetchone()
    )
    return None if row is None else float(row[0])


async def _apply(
    env: Env, did: int, action: feedback.Action, user_id: int
) -> feedback.FeedbackResult:
    return await env.db.write(lambda c: feedback.apply(c, did, action, user_id))


async def test_accept_bumps_tapper_pref_only(env: Env) -> None:
    cid = await seed_category(env, "dinner", [("Pho", [])])
    did = await _suggest(env, cid, "Pho", 1)
    r = await _apply(env, did, "accept", env.partner.id)
    assert r.applied and r.choice_text == "Pho"
    assert await _pref(env, 1, env.partner.id) == pytest.approx(1.05)
    assert await _pref(env, 1, env.jack.id) is None


async def test_reject_lowers_pref(env: Env) -> None:
    cid = await seed_category(env, "dinner", [("Pho", [])])
    await _apply(env, await _suggest(env, cid, "Pho", 1), "reject", env.jack.id)
    assert await _pref(env, 1, env.jack.id) == pytest.approx(0.85)


async def test_prefs_are_clamped(env: Env) -> None:
    cid = await seed_category(env, "dinner", [("Pho", []), ("Laksa", [])])
    for _ in range(40):
        await _apply(env, await _suggest(env, cid, "Pho", 1), "accept", env.jack.id)
        await _apply(env, await _suggest(env, cid, "Laksa", 2), "reject", env.jack.id)
    assert await _pref(env, 1, env.jack.id) == pytest.approx(3.0)
    assert await _pref(env, 2, env.jack.id) == pytest.approx(0.1)


async def test_double_tap_is_noop(env: Env) -> None:
    cid = await seed_category(env, "dinner", [("Pho", [])])
    did = await _suggest(env, cid, "Pho", 1)
    assert (await _apply(env, did, "accept", env.jack.id)).applied
    assert not (await _apply(env, did, "reject", env.partner.id)).applied
    assert await _pref(env, 1, env.jack.id) == pytest.approx(1.05)
    assert await _pref(env, 1, env.partner.id) is None
    assert not (await _apply(env, 9999, "accept", env.jack.id)).applied


async def test_accepting_generated_candidate_persists_option(env: Env) -> None:
    cid = await seed_category(env, "dinner")
    ctx = PickRequest(cid, "both", extra_candidates=[ExtraCandidate("Ramen", ["noodles"])])
    did = await _suggest(env, cid, "Ramen", None, ctx)
    r = await _apply(env, did, "accept", env.jack.id)
    assert r.option_id is not None
    row = await env.db.read(
        lambda c: c.execute("SELECT * FROM options WHERE id = ?", (r.option_id,)).fetchone()
    )
    assert (row["name"], row["tags_json"], row["owner"], row["created_by"]) == (
        "Ramen", '["noodles"]', "shared", "bot",
    )  # fmt: skip
    linked = await env.db.read(
        lambda c: c.execute("SELECT option_id FROM decisions WHERE id = ?", (did,)).fetchone()
    )
    assert linked[0] == r.option_id
    assert await _pref(env, r.option_id, env.jack.id) == pytest.approx(1.05)


async def test_reroll_returns_original_request(env: Env) -> None:
    cid = await seed_category(env, "dinner", [("Pho", [])])
    ctx = PickRequest(cid, "jack", n=2, exclude_tags=["spicy"])
    r = await _apply(env, await _suggest(env, cid, "Pho", 1, ctx), "reroll", env.jack.id)
    assert r.request == ctx
    assert await _pref(env, 1, env.jack.id) is None
