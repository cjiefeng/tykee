"""M9 done-when (§16): "Brunch around Tiong Bahru, pet friendly" returns 3 picks with Map links
and labelled pet-friendly sources; a user-confirmed attribute beats a web one; "near home" never
uses an exact address. Plus the gazetteer, ranking math, web finds, buttons and harvester."""

from __future__ import annotations

import json
import random
import sqlite3
from datetime import timedelta
from typing import Any

import pytest
from aiogram.types import CallbackQuery

from app import inbox_appliers
from app.harvest import Harvester
from app.places import areas as area_db
from app.places import attributes as attrs
from app.places import pets as pets_mod
from app.places.recommend import (
    TRUST_MAYBE,
    TRUST_WEB,
    Centre,
    check_must,
    choose,
    distance_fit,
)
from app.settings import set_value
from app.telegram.addressing import BotIdentity
from app.telegram.topics import KEY_ANSWER
from app.timeutil import to_sql
from tests.conftest import (
    GROUP_ID,
    JACK_TG,
    NOW,
    PARTNER_TG,
    TZ,
    Env,
    Stack,
    make_stack,
    mention,
    seed_category,
    tg_message,
    tg_user,
)
from tests.fakes.fake_llm import FakeLLMClient, tool_call

BOT = BotIdentity(id=777, username="TykeeBot")
BRUNCH = {
    "phrase": "brunch",
    "proposed_slug": "brunch",
    "description": "where to have brunch",
    "proposed_tau_days": 7,
}
TB = (1.28388, 103.83139)  # Tiong Bahru subzone centre in the seed


async def rows(env: Env, sql: str, *args: Any) -> list[sqlite3.Row]:
    return await env.db.read(lambda c: c.execute(sql, args).fetchall())


async def add_place(
    env: Env,
    name: str,
    lat: float | None,
    lng: float | None,
    *,
    category: int | None,
    visited_days_ago: float | None = None,
    pet: tuple[str, str, str] | None = None,  # (value, source, evidence)
) -> int:
    """A known place, an option of ``category``, optionally visited and with pet info."""

    def _go(c: sqlite3.Connection) -> int:
        cur = c.execute(
            "INSERT INTO places(name, name_norm, lat, lng, maps_url, first_seen_at, "
            "last_seen_at, visit_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                name.casefold(),
                lat,
                lng,
                f"https://maps.example/{name.replace(' ', '+')}",
                to_sql(NOW),
                to_sql(NOW),
                1 if visited_days_ago is not None else 0,
            ),
        )
        pid = int(cur.lastrowid or 0)
        if category is not None:
            c.execute(
                "INSERT INTO options(category_id, name, tags_json, place_id) VALUES (?, ?, ?, ?)",
                (category, name, '["place"]', pid),
            )
            if visited_days_ago is not None:
                c.execute(
                    "INSERT INTO decisions(category_id, choice_text, for_users, asked_by, status, "
                    "source, chat_id, created_at, place_id) "
                    "VALUES (?, ?, 'both', 1, 'accepted', 'user', ?, ?, ?)",
                    (
                        category,
                        name,
                        GROUP_ID,
                        to_sql(NOW - timedelta(days=visited_days_ago)),
                        pid,
                    ),
                )
        if pet is not None:
            attrs.put(
                c,
                pid,
                "pet_friendly",
                pet[0],
                source=pet[1],  # type: ignore[arg-type]
                evidence=pet[2],
                now=NOW - timedelta(days=30),
            )
        return pid

    return await env.db.write(_go)


async def brunch_places(env: Env) -> tuple[int, dict[str, int]]:
    cat = await seed_category(env, "brunch", tau=7)
    ids = {
        "Merci Marcel": await add_place(
            env,
            "Merci Marcel",
            1.2840,
            103.8320,
            category=cat,
            visited_days_ago=150,
            pet=("outdoor_only", "user", "only outside"),
        ),
        "Ottomani": await add_place(
            env,
            "Ottomani",
            1.2850,
            103.8330,
            category=cat,
            visited_days_ago=42,
            pet=("yes", "web", "dogs welcome https://www.sniffy.sg/ottomani"),
        ),
        "Plain Vanilla": await add_place(env, "Plain Vanilla", 1.2830, 103.8310, category=cat),
        "Tiong Bahru Bakery": await add_place(
            env, "Tiong Bahru Bakery", 1.2845, 103.8325, category=cat, pet=("no", "user", "")
        ),
        "Far Cafe": await add_place(env, "Far Cafe", 1.3500, 103.8400, category=cat),
        "Bar Nearby": await add_place(env, "Bar Nearby", 1.2841, 103.8321, category=None),
    }
    return cat, ids


async def setup(env: Env, *replies: Any, web: bool = False) -> Stack:
    await env.db.write(lambda c: set_value(c, "web.enabled", web))
    stack = make_stack(env, FakeLLMClient(*replies))
    inbox_appliers.register(stack.memory, stack.decisions, stack.places)
    return stack


async def ask(stack: Stack, env: Env, text: str) -> None:
    body, ents = mention(text)
    await stack.adapter.handle_message(tg_message(body, entities=ents), env.jack)


def cb(data: str, message_id: int) -> CallbackQuery:
    msg = tg_message("bot reply", from_id=BOT.id, message_id=message_id)
    return CallbackQuery(
        id="cb", from_user=tg_user(JACK_TG), chat_instance="ci", data=data, message=msg
    )


def result(stack: Stack, request: int) -> dict[str, Any]:
    (r,) = stack.llm.tool_results(request)
    return dict(json.loads(r["content"]))


def find(**kw: Any) -> Any:
    return tool_call("find_places", category="brunch", **kw)


# --- done-when -------------------------------------------------------------------------------


async def test_done_when_pet_friendly_brunch_around_tiong_bahru(env: Env) -> None:
    await brunch_places(env)
    stack = await setup(
        env,
        tool_call("resolve_category", **BRUNCH),
        find(area="Tiong Bahru", must=["pet_friendly"]),
        "3 picks near Tiong Bahru 🐶 …",
    )
    await pets_mod.save(stack.store, [pets_mod.Pet("Mochi", "dog", "small")])
    await ask(stack, env, "brunch around Tiong Bahru, pet friendly")

    out = result(stack, 2)
    assert out["area"] == "Tiong Bahru"
    picks = {p["name"]: p for p in out["picks"]}
    # Bakery says no pets, Far Cafe is outside the area, Bar Nearby isn't a brunch place.
    assert set(picks) == {"Merci Marcel", "Ottomani", "Plain Vanilla"}
    assert [p["n"] for p in out["picks"]] == [1, 2, 3]
    merci, otto, vanilla = picks["Merci Marcel"], picks["Ottomani"], picks["Plain Vanilla"]
    assert merci["must_haves"]["pet_friendly"] == "🐶 outdoor seating only (you confirmed)"
    assert otto["must_haves"]["pet_friendly"].startswith("🐶 pets OK (per sniffy.sg, checked")
    assert otto["must_haves"]["pet_friendly"].endswith("call ahead)")
    assert otto["last_went"] == "6 weeks ago"
    assert vanilla["maybe"] is True and vanilla["new_to_you"] is True  # web off: shortfall
    assert vanilla["must_haves"]["pet_friendly"] == "🐶 couldn't confirm"
    for p in out["picks"]:
        assert p["maps_url"] and f"[Map]({p['maps_url']})" in p["line"]

    # Pets are in the dynamic context, so "bringing Mochi" implies pet_friendly.
    assert "Pets: Mochi (small dog)" in str(stack.llm.requests[0].system[-1])

    (sent,) = stack.gateway.sent
    assert sent.keyboard is not None
    labels = [[b.text for b in row] for row in sent.keyboard]
    assert labels == [["✅ 1", "✅ 2", "✅ 3"], ["🎲 more"]]
    shown = await rows(env, "SELECT * FROM decisions WHERE status = 'suggested' ORDER BY id")
    assert [r["choice_text"] for r in shown] == [p["name"] for p in out["picks"]]
    assert all(r["place_id"] and r["tg_message_id"] == sent.message_id for r in shown)


async def test_user_confirmed_attribute_beats_web(env: Env) -> None:
    cat = await seed_category(env, "brunch")
    pid = await add_place(env, "Merci Marcel", *TB, category=cat)
    stack = await setup(env)
    places = stack.places

    await places.set_attribute(pid, "pet_friendly", "no", source="web", evidence="site A")
    a = await places.set_attribute(pid, "pet_friendly", "outdoor_only", source="user", evidence="")
    assert (a.value, a.source) == ("outdoor_only", "user")
    b = await places.set_attribute(pid, "pet_friendly", "yes", source="web", evidence="site B")
    assert (b.value, b.source) == ("outdoor_only", "user")  # web never overrides you

    # Two web sources that disagree → unknown, both evidences kept.
    other = await add_place(env, "Ottomani", *TB, category=cat)
    await places.set_attribute(other, "pet_friendly", "yes", source="web", evidence="site A")
    c = await places.set_attribute(other, "pet_friendly", "no", source="web", evidence="site B")
    assert (c.value, c.evidence) == ("unknown", "site A | site B")


async def test_near_home_uses_the_neighbourhood_never_an_address(env: Env) -> None:
    cat = await seed_category(env, "brunch")
    bishan = await env.db.read(lambda c: area_db.match(c, "Bishan"))
    assert bishan is not None
    await add_place(env, "Bishan Cafe", bishan.lat + 0.001, bishan.lng, category=cat)
    stack = await setup(
        env,
        tool_call("resolve_category", **BRUNCH),
        find(area="near home"),
        "Sorry, which area?",
        tool_call("resolve_category", **BRUNCH),
        find(area="near home"),
        "Bishan Cafe!",
    )
    await ask(stack, env, "brunch near home")
    (err,) = stack.llm.tool_results(2)
    assert err.get("is_error") and "home area" in str(err["content"])

    with pytest.raises(area_db.AreaError):  # a home area can only copy a known area
        await env.db.write(lambda c: area_db.save_user_area(c, "Home", "1.35, 103.84", []))
    area, base = await env.db.write(
        lambda c: area_db.save_user_area(c, "Home", "Bishan", ["home", "us"])
    )
    assert (area.lat, area.lng, area.radius_m, base.name) == (
        bishan.lat,
        bishan.lng,
        bishan.radius_m,
        "Bishan",
    )
    await ask(stack, env, "brunch near home")
    out = result(stack, 5)
    assert out["area"] == "Home" and [p["name"] for p in out["picks"]] == ["Bishan Cafe"]
    (d,) = await rows(env, "SELECT context_json FROM decisions WHERE status = 'suggested'")
    centre = json.loads(d[0])["recommend"]["centre"]
    assert (centre["lat"], centre["lng"]) == (bishan.lat, bishan.lng)
    # The user area is never stored as a place's area.
    assert await env.db.read(lambda c: area_db.in_text(c, "home")) is None


# --- web discovery -----------------------------------------------------------------------------


async def test_web_discovery_fills_the_explore_slot(env: Env) -> None:
    _, ids = await brunch_places(env)
    stack = await setup(
        env,
        tool_call("resolve_category", **BRUNCH),
        find(area="tb", must=["pet_friendly"]),
        tool_call(
            "save_place_candidates",
            category="brunch",
            candidates=[
                {
                    "name": "Ottomani",  # already known: deduped, already a pick
                    "source_url": "https://blog.example/tb",
                },
                {
                    "name": "Woof Cafe",
                    "source_url": "https://www.sniffy.sg/woof",
                    "address": "78 Yong Siak St, Tiong Bahru",
                    "attributes": [
                        {"key": "pet_friendly", "value": "yes", "quote": "dogs welcome inside"}
                    ],
                },
            ],
        ),
        "picks …",
        web=True,
    )
    await ask(stack, env, "brunch around TB with Mochi")

    out = result(stack, 2)
    assert out["suggest_web"] is True and "save_place_candidates" in out["note"]
    # Both known pet-friendly places have been visited; the explore slot is left for the web.
    assert [p["name"] for p in out["picks"]] == ["Merci Marcel", "Ottomani"]

    saved = result(stack, 3)
    (new,) = saved["new_picks"]
    assert (new["n"], new["name"], new["new_to_you"]) == (3, "Woof Cafe", True)
    assert new["must_haves"]["pet_friendly"].startswith("🐾 pets OK (per sniffy.sg")
    assert saved["skipped"] == ["Ottomani: already suggested"]
    (woof,) = await rows(env, "SELECT * FROM places WHERE name = 'Woof Cafe'")
    assert woof["status"] == "unvisited" and woof["lat"] is None and woof["note_path"] is None
    assert woof["maps_url"].startswith("https://www.google.com/maps/search/?api=1&query=Woof")
    tb = await env.db.read(lambda c: area_db.match(c, "Tiong Bahru"))
    assert tb is not None and woof["area_id"] == tb.id
    (a,) = await rows(env, "SELECT * FROM place_attributes WHERE place_id = ?", woof["id"])
    assert (a["value"], a["source"]) == ("yes", "web")
    assert a["evidence"] == "dogs welcome inside https://www.sniffy.sg/woof"
    assert len(await rows(env, "SELECT * FROM places WHERE name = 'Ottomani'")) == 1
    assert ids["Ottomani"]  # unchanged row

    (sent,) = stack.gateway.sent
    assert [b.text for b in sent.keyboard[0]] == ["✅ 1", "✅ 2", "✅ 3"]  # type: ignore[index]

    # ✅ 3: the web find becomes visited, a brunch option and gets its note.
    await stack.adapter.handle_callback(cb(sent.keyboard[0][2].data, sent.message_id), env.jack)  # type: ignore[index]
    assert stack.gateway.keyboards[sent.message_id] is None
    (woof,) = await rows(env, "SELECT * FROM places WHERE name = 'Woof Cafe'")
    assert (woof["status"], woof["visit_count"]) == ("visited", 1)
    (opt,) = await rows(env, "SELECT * FROM options WHERE place_id = ?", woof["id"])
    assert opt["name"] == "Woof Cafe"
    note = await stack.store.read(woof["note_path"])
    assert note is not None and "Visits recorded: 1" in note.body
    assert "sniffy" not in note.body  # web labels stay out of the vault
    assert stack.gateway.sent[-1].text == "✅ **Woof Cafe** it is."


async def test_web_finds_need_find_places_first(env: Env) -> None:
    await seed_category(env, "brunch")
    stack = await setup(
        env,
        tool_call(
            "save_place_candidates",
            category="brunch",
            candidates=[{"name": "X", "source_url": "https://x.example"}],
        ),
        "ok",
    )
    await ask(stack, env, "save it")
    (r,) = stack.llm.tool_results(1)
    assert r.get("is_error") and "find_places" in str(r["content"])
    assert await rows(env, "SELECT * FROM places") == []


# --- 🎲 more -----------------------------------------------------------------------------------


async def test_more_never_repeats_and_ends_politely(env: Env) -> None:
    await brunch_places(env)
    stack = await setup(
        env,
        tool_call("resolve_category", **BRUNCH),
        find(area="Tiong Bahru", n=2),
        "two picks",
    )
    await ask(stack, env, "brunch around tiong bahru")
    first = {p["name"] for p in result(stack, 2)["picks"]}
    sent = stack.gateway.sent[-1]
    more = sent.keyboard[1][0].data  # type: ignore[index]

    await stack.adapter.handle_more(cb(more, sent.message_id), env.jack)
    assert stack.gateway.keyboards[sent.message_id] is None
    second = stack.gateway.sent[-1]
    assert second.text.startswith("🎲 More around Tiong Bahru:")
    names = [line.split("**")[1] for line in second.text.splitlines()[1:]]
    assert len(names) == 2 and not first & set(names)
    assert [b.text for b in second.keyboard[0]] == ["✅ 1", "✅ 2"]  # type: ignore[index]
    old = await rows(env, "SELECT status FROM decisions WHERE choice_text IN (?, ?)", *first)
    assert {r[0] for r in old} == {"rerolled"}

    # Merci, Ottomani, Plain Vanilla and Bakery (no must-haves) are all shown now.
    await stack.adapter.handle_more(cb(second.keyboard[1][0].data, second.message_id), env.jack)  # type: ignore[index]
    assert stack.gateway.sent[-1].text.startswith("That's every place I know around Tiong Bahru")


# --- set_place_attribute + harvester ------------------------------------------------------------


async def test_set_place_attribute_is_user_sourced_and_noted(env: Env) -> None:
    cat = await seed_category(env, "brunch")
    pid = await add_place(env, "Merci Marcel", *TB, category=cat)
    stack = await setup(
        env,
        tool_call(
            "set_place_attribute",
            place_id=pid,
            key="pet_friendly",
            value="outdoor only",
            evidence="small dogs only, outside",
        ),
        "Noted 🐶",
    )
    await stack.places.visit(pid)
    await ask(stack, env, "merci marcel only lets small dogs sit outside")
    (a,) = await rows(env, "SELECT * FROM place_attributes")
    assert (a["value"], a["source"], a["evidence"]) == (
        "outdoor_only",
        "user",
        "small dogs only, outside",
    )
    (place,) = await rows(env, "SELECT note_path FROM places")
    note = await stack.store.read(place[0])
    assert note is not None
    assert "- Pet-friendly: outdoor seating only (small dogs only, outside), confirmed" in note.body


async def test_harvester_suggests_place_info_for_approval(env: Env) -> None:
    await env.db.write(lambda c: set_value(c, KEY_ANSWER, 5))
    await env.db.write(lambda c: set_value(c, "harvest.min_new_messages", 1))
    cat = await seed_category(env, "brunch")
    pid = await add_place(env, "Merci Marcel", *TB, category=cat)
    reply = json.dumps(
        {
            "episodes": [],
            "facts": [],
            "options": [],
            "place_attributes": [
                {
                    "place": "Merci Marcel",
                    "key": "pet_friendly",
                    "value": "yes",
                    "quote": "brought Mochi to merci marcel, they had a water bowl",
                },
                {"place": "Nowhere Known", "key": "pet_friendly", "value": "yes", "quote": ""},
            ],
            "skipped_out_of_scope": 0,
        }
    )
    stack = await setup(env, reply)
    h = Harvester(
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
    await stack.adapter.handle_message(
        tg_message(
            "brought Mochi to merci marcel, they had a water bowl",
            from_id=PARTNER_TG,
            topic=9,
            forum=True,
        ),
        env.partner,
    )
    (run,) = await h.tick(force=True)
    assert run.suggestions == 1  # the unknown place is ignored
    (item,) = await stack.memory.pending()
    assert item.kind == "attribute" and item.content == "Merci Marcel: pet-friendly → pets OK"
    assert await rows(env, "SELECT * FROM place_attributes") == []
    await stack.memory.decide(item.id, approve=True, user_id=env.jack.id)
    (a,) = await rows(env, "SELECT * FROM place_attributes WHERE place_id = ?", pid)
    assert (a["value"], a["source"]) == ("yes", "user")


# --- gazetteer -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("Tiong Bahru", "Tiong Bahru"),
        ("around TB", "Tiong Bahru"),
        ("中峇鲁", "Tiong Bahru"),
        ("tiong bahru mrt", "Tiong Bahru MRT"),
        ("Tiong Baru", "Tiong Bahru"),  # fuzzy ≥ 90
        ("near Holland V", "Holland Village"),
        ("AMK", "Ang Mo Kio"),
        ("bukit panjang station", "Bukit Panjang MRT"),
        ("Orchard Road area", "Orchard"),
    ],
)
async def test_area_matching(env: Env, text: str, name: str) -> None:
    area = await env.db.read(lambda c: area_db.match(c, text))
    assert area is not None and area.name == name


async def test_unknown_area_and_seed_is_idempotent(env: Env) -> None:
    assert await env.db.read(lambda c: area_db.match(c, "Atlantis")) is None
    counts = await rows(env, "SELECT kind, count(*) FROM areas GROUP BY kind ORDER BY kind")
    assert {r[0] for r in counts} == {"mrt", "neighbourhood", "planning_area", "subzone"}
    assert await env.db.write(area_db.seed_areas) == 0


# --- ranking math ------------------------------------------------------------------------------


def test_distance_fit() -> None:
    assert distance_fit(0, 1000) == 1.0 and distance_fit(500, 1000) == 1.0
    assert distance_fit(1000, 1000) == pytest.approx(0.3)
    assert distance_fit(750, 1000) == pytest.approx(0.65)
    assert distance_fit(1001, 1000) is None


def _attr(value: str, source: str, days_old: float = 1) -> attrs.Attribute:
    checked = to_sql(NOW - timedelta(days_old))
    return attrs.Attribute(1, "pet_friendly", value, source, None, checked)


def test_must_have_filter() -> None:
    must = ["pet_friendly"]
    ok = check_must({"pet_friendly": _attr("outdoor_only", "user")}, must, NOW, 180)
    assert ok is not None and ok.passes and ok.trust == 1.0
    web = check_must({"pet_friendly": _attr("yes", "web")}, must, NOW, 180)
    assert web is not None and web.passes and web.trust == TRUST_WEB
    stale = check_must({"pet_friendly": _attr("yes", "web", days_old=400)}, must, NOW, 180)
    assert stale is not None and stale.maybe and stale.trust == TRUST_MAYBE
    unknown = check_must({}, must, NOW, 180)
    assert unknown is not None and unknown.maybe
    assert check_must({"pet_friendly": _attr("no", "user")}, must, NOW, 180) is None
    assert check_must({"pet_friendly": _attr("no", "user")}, [], NOW, 180) is not None


def test_choose_reserves_explore_slots() -> None:
    from app.places.recommend import MustCheck, Scored
    from app.places.service import Place

    def scored(i: int, new: bool) -> Scored:
        p = Place(i, f"P{i}", None, None, None, None, "u", None, "t", "t", 0 if new else 3)
        ok = MustCheck(True, False, 1, {})
        return Scored(p, None, 1.0, 1.0, 1.0, 1.0, None if new else "t", ok)

    known = [scored(i, False) for i in range(5)]
    picks, unfilled = choose([*known, scored(9, True)], 3, 1, random.Random(1))
    assert len(picks) == 3 and picks[-1].place.id == 9 and unfilled == 0
    picks, unfilled = choose(known, 3, 1, random.Random(1))
    assert len(picks) == 3 and unfilled == 1  # backfilled with known places
    picks, unfilled = choose(known, 3, 1, random.Random(1), backfill=False)
    assert len(picks) == 2 and unfilled == 1  # the slot waits for a web find


def test_centre_round_trips_through_context() -> None:
    from app.places.recommend import RecommendRequest

    req = RecommendRequest(3, "both", Centre("TB", 1.0, 2.0, 900, 7, "Tiong Bahru"), ["halal"], 2)
    assert RecommendRequest.from_context(req.to_json()) == req
    assert RecommendRequest.from_context('{"category_id": 1}') is None


async def test_merge_keeps_user_attributes_and_delete_cascades(env: Env) -> None:
    cat = await seed_category(env, "brunch")
    src = await add_place(env, "Merci", *TB, category=cat, pet=("outdoor_only", "user", "said"))
    dst = await add_place(env, "Merci Marcel", *TB, category=None, pet=("yes", "web", "site"))
    stack = await setup(env)
    await stack.places.merge(src, dst)
    (a,) = await rows(env, "SELECT * FROM place_attributes")
    assert (a["place_id"], a["value"], a["source"]) == (dst, "outdoor_only", "user")
    await stack.places.delete(dst)
    assert await rows(env, "SELECT * FROM place_attributes") == []
