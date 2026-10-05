"""Plain-name matching (§10.5, v1.41): a shop named in words, without a link, is linked to the
known place for decisions, options, attributes and "near X"; past rows are backfilled once,
exact matches only."""

from __future__ import annotations

import sqlite3
from typing import Any

from app import inbox_appliers
from app.harvest import Harvester
from app.places import links
from app.places.links import NameMatch
from app.places.service import NAME_BACKFILL_DONE
from app.settings import get_value, set_value
from app.telegram.topics import KEY_ANSWER
from tests.conftest import GROUP_ID, TZ, Env, Stack, make_stack, seed_category
from tests.fakes.fake_llm import FakeLLMClient, make_message, tool_call
from tests.unit.test_harvest import episode, extraction
from tests.unit.test_place_flow import ANSWER, FOOD, say
from tests.unit.test_recommend import BRUNCH, TB, add_place, ask, find, result, setup

DINNER = {
    "phrase": "dinner",
    "proposed_slug": "dinner",
    "description": "where to have dinner",
    "proposed_tau_days": 3,
}


async def rows(env: Env, sql: str, *args: Any) -> list[sqlite3.Row]:
    return await env.db.read(lambda c: c.execute(sql, args).fetchall())


# --- pure matching ---------------------------------------------------------------------------

PLACES = [
    (1, "Keisuke Tonkotsu King"),
    (2, "Din Tai Fung"),
    (3, "Merci Marcel"),
    (4, "Kopitiam"),
    (5, "Kopitiam"),
]


def test_tiers_exact_fuzzy_prefix() -> None:
    assert links.match_name("merci marcel", PLACES) == NameMatch(3)
    assert links.match_name("Merci Marcell", PLACES) == NameMatch(3)  # fuzzy ≥ 90
    assert links.match_name("Keisuke", PLACES) == NameMatch(1)  # leading tokens
    assert links.match_name("keisuke tonkotsu", PLACES) == NameMatch(1)
    assert links.match_name("tonkotsu king", PLACES) == NameMatch()  # not a prefix
    assert links.match_name("din", PLACES) == NameMatch()  # prefix needs 5 characters
    assert links.match_name("", PLACES) == NameMatch()


def test_first_tier_with_a_hit_decides_and_linked_narrows() -> None:
    # The exact hit wins over a prefix hit on a longer name.
    named = [(1, "Keisuke"), (2, "Keisuke Tonkotsu King")]
    assert links.match_name("keisuke", named) == NameMatch(1)
    assert links.match_name("Kopitiam", PLACES) == NameMatch(ambiguous=(4, 5))
    assert links.match_name("Kopitiam", PLACES, linked={5}) == NameMatch(5)
    assert links.match_name("Kopitiam", PLACES, linked={4, 5}) == NameMatch(ambiguous=(4, 5))


async def test_service_prefers_the_categorys_option(env: Env) -> None:
    dinner = await seed_category(env, "dinner")
    a = await add_place(env, "Keisuke", *TB, category=None)
    b = await add_place(env, "Keisuke", *TB, category=dinner)  # another outlet
    stack = await setup(env)
    assert await stack.places.match_name("keisuke") == NameMatch(ambiguous=(a, b))
    assert await stack.places.match_name("keisuke", dinner) == NameMatch(b)
    # An option with a different name but a place: matched by the option first.
    await env.db.write(
        lambda c: c.execute(
            "INSERT INTO options(category_id, name, place_id) VALUES (?, 'the ramen place', ?)",
            (dinner, a),
        )
    )
    assert await stack.places.match_name("The Ramen Place", dinner) == NameMatch(a)


# --- tools -----------------------------------------------------------------------------------


async def test_record_decision_by_name_links_the_place(env: Env) -> None:
    await seed_category(env, "dinner")
    pid = await add_place(env, "Keisuke Tonkotsu King", *TB, category=None)
    stack = await setup(
        env,
        tool_call("resolve_category", **DINNER),
        tool_call("record_decision", category="dinner", choice="keisuke"),
        "",
    )
    await ask(stack, env, "dinner at keisuke tonight")

    (d,) = await rows(env, "SELECT * FROM decisions")
    assert (d["choice_text"], d["source"], d["place_id"]) == ("Keisuke Tonkotsu King", "user", pid)
    (p,) = await rows(env, "SELECT visit_count FROM places")
    assert p[0] == 1
    assert result(stack, 2)["choice"] == "Keisuke Tonkotsu King"


async def test_record_decision_ambiguous_records_nothing(env: Env) -> None:
    await seed_category(env, "dinner")
    await add_place(env, "Kopitiam", *TB, category=None)
    await add_place(env, "Kopitiam", 1.35, 103.9, category=None)
    stack = await setup(
        env,
        tool_call("resolve_category", **DINNER),
        tool_call("record_decision", category="dinner", choice="kopitiam"),
        "Which Kopitiam?",
    )
    await ask(stack, env, "dinner at kopitiam")

    (r,) = stack.llm.tool_results(2)
    assert r.get("is_error") and "several known places match" in str(r["content"])
    assert "place_id 1: Kopitiam; place_id 2: Kopitiam" in str(r["content"])
    assert await rows(env, "SELECT * FROM decisions") == []


async def test_add_option_by_name_links_the_place(env: Env) -> None:
    dinner = await seed_category(env, "dinner")
    pid = await add_place(env, "Keisuke Tonkotsu King", *TB, category=None)
    stack = await setup(
        env,
        tool_call("resolve_category", **DINNER),
        make_message(
            tool_calls=[("add_option", {"category": "dinner", "name": "Keisuke Tonkotsu King"})]
        ),
        "Added.",
    )
    await ask(stack, env, "add keisuke tonkotsu king to dinner")
    (o,) = await rows(env, "SELECT place_id FROM options WHERE category_id = ?", dinner)
    assert o[0] == pid


async def test_find_places_near_a_place_named_in_words(env: Env) -> None:
    cat = await seed_category(env, "brunch")
    await add_place(env, "Merci Marcel", *TB, category=cat)
    await add_place(env, "Plain Vanilla", 1.2830, 103.8310, category=cat)
    stack = await setup(
        env,
        tool_call("resolve_category", **BRUNCH),
        find(near_place="merci marcel"),
        "1 pick near Merci Marcel",
        tool_call("resolve_category", **BRUNCH),
        find(near_place="Nowhere Cafe"),
        "ok",
    )
    await ask(stack, env, "brunch near merci marcel")
    out = result(stack, 2)
    assert out["area"] == "Merci Marcel"
    assert [p["name"] for p in out["picks"]] == ["Plain Vanilla"]  # the anchor is skipped

    await ask(stack, env, "brunch near nowhere cafe")
    (r,) = stack.llm.tool_results(5)
    assert r.get("is_error") and "unknown place 'Nowhere Cafe'" in str(r["content"])


# --- harvester -------------------------------------------------------------------------------


def harvester(env: Env, stack: Stack) -> Harvester:
    return Harvester(
        db=env.db,
        settings=env.settings,
        llm=stack.llm,
        memory=stack.memory,
        decisions=stack.decisions,
        topics=stack.topics,
        users=env.users,
        tz=TZ,
        group_id=lambda: GROUP_ID,
        places=stack.places,
        clock=stack.clock,
    )


async def test_harvester_links_a_plain_name_without_a_link(env: Env) -> None:
    await env.db.write(lambda c: set_value(c, KEY_ANSWER, ANSWER))
    await env.db.write(lambda c: set_value(c, "harvest.min_new_messages", 1))
    await seed_category(env, "dinner")
    pid = await add_place(env, "Keisuke Tonkotsu King", *TB, category=None)
    option = {"category_phrase": "dinner", "name": "Keisuke", "sentiment": 0.8, "tags": []}
    reply = extraction(episodes=[episode("dinner", "keisuke")], options=[option])
    stack = make_stack(env, FakeLLMClient(reply))
    inbox_appliers.register(stack.memory, stack.decisions, stack.places)
    await say(stack, env, "keisuke for dinner tonight?", topic=FOOD)

    (run,) = await harvester(env, stack).tick(force=True)
    assert run.decisions == 1
    (d,) = await rows(env, "SELECT * FROM decisions")
    assert (d["choice_text"], d["source"], d["place_id"]) == (
        "Keisuke Tonkotsu King",
        "observed",
        pid,
    )
    (p,) = await rows(env, "SELECT visit_count FROM places")
    assert p[0] == 1
    # The decision made the place an option, so the option isn't suggested again.
    assert run.suggestions == 0


# --- backfill --------------------------------------------------------------------------------


async def test_backfill_links_exact_names_once(env: Env) -> None:
    dinner = await seed_category(env, "dinner")
    keisuke = await add_place(env, "Keisuke", *TB, category=None)
    await add_place(env, "Kopitiam", *TB, category=None)
    await add_place(env, "Kopitiam", 1.35, 103.9, category=None)

    def _seed(c: sqlite3.Connection) -> None:
        for choice, status in [
            ("keisuke", "accepted"),
            ("Keisuke", "accepted"),
            ("Keisuke", "rejected"),  # not a visit
            ("Keisuke Ramen", "accepted"),  # prefix/fuzzy: never retroactive
            ("Kopitiam", "accepted"),  # two places: ambiguous
        ]:
            c.execute(
                "INSERT INTO decisions(category_id, choice_text, for_users, asked_by, status, "
                "source, created_at) VALUES (?, ?, 'both', 1, ?, 'user', '2026-09-01 12:00:00')",
                (dinner, choice, status),
            )
        c.execute("INSERT INTO options(category_id, name) VALUES (?, 'KEISUKE')", (dinner,))

    await env.db.write(_seed)
    stack = await setup(env)
    assert await stack.places.backfill_names() == (1, 2)
    linked = await rows(
        env, "SELECT choice_text, status FROM decisions WHERE place_id = ?", keisuke
    )
    assert sorted(tuple(r) for r in linked) == [("Keisuke", "accepted"), ("keisuke", "accepted")]
    (o,) = await rows(env, "SELECT place_id FROM options")
    assert o[0] == keisuke
    place = await stack.places.get(keisuke)
    assert place is not None and place.visit_count == 2 and place.note_path
    assert await env.db.read(lambda c: get_value(c, NAME_BACKFILL_DONE)) is True

    assert await stack.places.backfill_names() == (0, 0)  # once per install
    place = await stack.places.get(keisuke)
    assert place is not None and place.visit_count == 2
