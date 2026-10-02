"""M8 done-when (§16): pasting a Maps link with "eating here" in #Tykee gets a reaction, "where
are we eating?" answers with the shop name and link, and a link to someone's home is never
stored. Plus venue/location messages, the record_decision tool, other topics and dashboards."""

from __future__ import annotations

import sqlite3
from typing import Any

from aiogram.types import Location, MessageEntity, Venue

from app import inbox_appliers
from app.settings import set_value
from app.telegram.topics import KEY_ANSWER
from tests.conftest import (
    GROUP_ID,
    JACK_TG,
    PARTNER_TG,
    Env,
    Stack,
    make_stack,
    mention,
    seed_category,
    tg_message,
    url_entities,
)
from tests.fakes.fake_llm import FakeLLMClient, tool_call
from tests.unit.test_place_links import HOME_Q, PLACE_PAGE, SHARED_Q

ANSWER, FOOD = 5, 9
SHORT = "https://maps.app.goo.gl/AbC123xyz"
HOME = "https://maps.app.goo.gl/HoMe999"
REDIRECTS = {SHORT: SHARED_Q, HOME: HOME_Q}


async def setup(env: Env, *replies: Any, dinner: bool = True) -> Stack:
    await env.db.write(lambda c: set_value(c, KEY_ANSWER, ANSWER))
    if dinner:
        await seed_category(env, "dinner", [("Mala", [])])
    stack = make_stack(env, FakeLLMClient(*replies), redirects=REDIRECTS)
    inbox_appliers.register(stack.memory, stack.decisions)
    return stack


async def say(
    stack: Stack,
    env: Env,
    text: str,
    *urls: str,
    topic: int | None = ANSWER,
    from_id: int = PARTNER_TG,
    entities: list[MessageEntity] | None = None,
) -> int:
    msg = tg_message(
        text,
        from_id=from_id,
        topic=topic,
        forum=True,
        entities=[*(entities or []), *url_entities(text, *urls)] or None,
    )
    user = env.partner if from_id == PARTNER_TG else env.jack
    await stack.adapter.handle_message(msg, user)
    return msg.message_id


async def rows(env: Env, sql: str, *args: Any) -> list[sqlite3.Row]:
    return await env.db.read(lambda c: c.execute(sql, args).fetchall())


async def stored_texts(env: Env) -> list[str]:
    from app.db.repos.messages import _text_of

    return [_text_of(r["content"]) for r in await rows(env, "SELECT content FROM messages")]


# --- done-when ---------------------------------------------------------------------------------


async def test_eating_here_link_is_recorded_with_a_reaction(env: Env) -> None:
    stack = await setup(env)
    text = f"eating here 👉 {SHORT}"
    msg_id = await say(stack, env, text, SHORT)

    assert stack.gateway.reactions == [(GROUP_ID, msg_id, "👌")]
    assert stack.gateway.sent == [] and stack.llm.requests == []  # silent, no LLM call
    (d,) = await rows(env, "SELECT * FROM decisions")
    assert (d["choice_text"], d["status"], d["source"], d["for_users"]) == (
        "Keisuke Tonkotsu King",
        "accepted",
        "user",
        "both",
    )
    (place,) = await rows(env, "SELECT * FROM places")
    assert d["place_id"] == place["id"] and place["visit_count"] == 1
    (opt,) = await rows(env, "SELECT * FROM options WHERE place_id = ?", place["id"])
    assert opt["name"] == "Keisuke Tonkotsu King" and opt["tags_json"] == '["place"]'
    cat = await rows(env, "SELECT slug FROM categories WHERE id = ?", d["category_id"])
    assert cat[0]["slug"] == "dinner"  # 19:00 local → the dinner meal slot

    (stored,) = await stored_texts(env)
    assert stored.startswith(f"eating here 👉 {SHORT} ⟦place: Keisuke Tonkotsu King · 1 Tras Link")
    assert stored.endswith(f"place_id={place['id']}⟧")
    note = await stack.store.read(place["note_path"])
    assert note is not None and note.type == "place" and "Visits recorded: 1" in note.body
    assert "Keisuke Tonkotsu King" in (await stack.store.read("logs/2026/10/2026-10-02.md")).body  # type: ignore[union-attr]

    # Same link again in the same session: no second decision, no second reaction.
    await say(stack, env, f"eating here {SHORT}", SHORT)
    assert len(await rows(env, "SELECT * FROM decisions")) == 1
    assert len(stack.gateway.reactions) == 1


async def test_where_are_we_eating_gets_name_and_link(env: Env) -> None:
    stack = await setup(env, "Keisuke Tonkotsu King, [here](maps)")
    await say(stack, env, f"eating here {SHORT}", SHORT)
    text, ents = mention("where are we eating?")
    await say(stack, env, text, entities=ents, from_id=JACK_TG)

    system = stack.llm.requests[-1].system
    dynamic = str(system[-1]["text"])  # type: ignore[index]
    assert "Decisions today (this chat):" in dynamic
    assert f"- Dinner: Keisuke Tonkotsu King (accepted, maps link: {SHORT})" in dynamic
    assert "record_decision" in str(system)  # place rules are in the cached block
    replay = str(stack.llm.requests[-1].messages)
    assert "⟦place: Keisuke Tonkotsu King" in replay  # the shared link's marker is in history


async def test_home_link_is_never_stored(env: Env) -> None:
    stack = await setup(env)
    await say(stack, env, f"come over 👉 {HOME} eating here", HOME)
    await say(stack, env, f"pin: {HOME_Q}", HOME_Q)

    assert await rows(env, "SELECT * FROM places") == []
    assert await rows(env, "SELECT * FROM decisions") == []
    assert stack.gateway.reactions == []
    texts = await stored_texts(env)
    assert texts == ["come over 👉 ⟦location shared⟧ eating here", "pin: ⟦location shared⟧"]
    assert all("Tampines" not in t and "maps" not in t for t in texts)
    # The short link is cached without its target; the full link (the address itself) isn't kept.
    links_ = await rows(env, "SELECT url, status, final_url FROM place_links")
    assert [tuple(r) for r in links_] == [(HOME, "unnamed", None)]
    assert not (env.vault / "shared" / "places").exists()


# --- intent split over two messages, no intent, other topics -----------------------------------


async def test_link_then_eating_here_reacts_on_the_link(env: Env) -> None:
    stack = await setup(env)
    link_id = await say(stack, env, f"this one? {SHORT}", SHORT)
    assert stack.gateway.reactions == []
    # Shared without deciding: offered as an option in the inbox.
    (item,) = await stack.memory.pending()
    assert item.kind == "option" and item.payload["place_id"] == 1

    await say(stack, env, "ok eating here", from_id=JACK_TG)
    assert stack.gateway.reactions == [(GROUP_ID, link_id, "👌")]
    (d,) = await rows(env, "SELECT * FROM decisions")
    assert d["place_id"] == 1 and d["asked_by"] == env.jack.id

    # Approving the stale suggestion later is harmless: the option already exists.
    await stack.memory.decide(item.id, approve=True, user_id=None)
    assert len(await rows(env, "SELECT * FROM options WHERE place_id = 1")) == 1


async def test_intent_before_link_and_named_category(env: Env) -> None:
    stack = await setup(env)
    await seed_category(env, "lunch")
    await say(stack, env, "lunch here tmr?")  # intent phrase that also names the category
    msg_id = await say(stack, env, SHORT, SHORT, from_id=JACK_TG)
    assert stack.gateway.reactions == [(GROUP_ID, msg_id, "👌")]
    (d,) = await rows(
        env, "SELECT c.slug FROM decisions d JOIN categories c ON c.id = d.category_id"
    )
    assert d["slug"] == "lunch"  # named beats the 19:00 dinner slot


async def test_no_category_records_nothing(env: Env) -> None:
    stack = await setup(env, dinner=False)
    await say(stack, env, f"eating here {SHORT}", SHORT)
    assert await rows(env, "SELECT * FROM decisions") == []
    assert stack.gateway.reactions == []
    assert len(await rows(env, "SELECT * FROM places")) == 1  # the place is still known


async def test_other_topic_annotates_but_never_reacts(env: Env) -> None:
    stack = await setup(env)
    await say(stack, env, f"eating here {SHORT}", SHORT, topic=FOOD)
    assert stack.gateway.reactions == [] and await rows(env, "SELECT * FROM decisions") == []
    (stored,) = await stored_texts(env)
    assert "⟦place: Keisuke Tonkotsu King" in stored  # the harvester sees the name


async def test_disabled_leaves_messages_alone(env: Env) -> None:
    stack = await setup(env)
    await env.db.write(lambda c: set_value(c, "places.enabled", False))
    await say(stack, env, f"eating here {SHORT}", SHORT)
    assert await stored_texts(env) == [f"eating here {SHORT}"]
    assert await rows(env, "SELECT * FROM place_links") == []


# --- Telegram venue / location messages ----------------------------------------------------------


async def test_venue_and_location_messages(env: Env) -> None:
    stack = await setup(env)
    venue = Venue(
        location=Location(latitude=1.2799, longitude=103.8443),
        title="Keisuke Tonkotsu King",
        address="1 Tras Link",
        google_place_id="ChIJabc",
    )
    msg = tg_message(None, from_id=PARTNER_TG, topic=ANSWER, venue=venue)
    await stack.adapter.handle_message(msg, env.partner)
    pin = tg_message(
        None, from_id=PARTNER_TG, topic=ANSWER, location=Location(latitude=1.35, longitude=103.8)
    )
    await stack.adapter.handle_message(pin, env.partner)
    home = Venue(
        location=Location(latitude=1.35, longitude=103.9),
        title="Blk 123 Tampines St 11",
        address="",
    )
    await stack.adapter.handle_message(
        tg_message(None, from_id=PARTNER_TG, topic=ANSWER, venue=home), env.partner
    )

    (place,) = await rows(env, "SELECT * FROM places")
    assert place["google_id"] == "gpid:ChIJabc" and place["lat"] == 1.2799
    assert "query_place_id=ChIJabc" in place["maps_url"]
    texts = await stored_texts(env)
    assert texts[0].startswith("[venue] Keisuke Tonkotsu King ⟦place: Keisuke Tonkotsu King · 1")
    assert texts[1:] == ["[location] ⟦location shared⟧"] * 2  # no coordinates, no address

    # The same shop shared later as a Maps link (~10 m away, other id scheme) is the same place.
    await say(stack, env, PLACE_PAGE, PLACE_PAGE)
    assert len(await rows(env, "SELECT * FROM places")) == 1


# --- addressed: Claude's record_decision -------------------------------------------------------


async def test_record_decision_tool_reacts_instead_of_replying(env: Env) -> None:
    stack = await setup(env)
    await say(stack, env, f"this place looks good {SHORT}", SHORT)  # place 1, no intent
    stack.gateway.reactions.clear()
    stack.llm._replies.extend(
        [tool_call("record_decision", category="dinner", choice="that ramen", place_id=1), ""]
    )
    text, ents = mention("ok we're going there tonight")
    await say(stack, env, text, entities=ents, from_id=JACK_TG)

    assert stack.gateway.sent == []  # the reaction is the answer
    (link_msg,) = await rows(
        env, "SELECT tg_message_id FROM messages WHERE role = 'user' AND content LIKE '%Keisuke%'"
    )
    assert stack.gateway.reactions == [(GROUP_ID, link_msg[0], "👌")]
    (d,) = await rows(env, "SELECT * FROM decisions")
    assert (d["choice_text"], d["source"], d["place_id"]) == ("Keisuke Tonkotsu King", "user", 1)
    result = stack.llm.tool_results(1)[0]
    assert '"recorded": true' in str(result["content"])


async def test_record_decision_tool_validates(env: Env) -> None:
    bad = tool_call("record_decision", category="dinner", choice="x", place_id=99)
    stack = await setup(env, bad, "Noted.")
    text, ents = mention("we went to that place")
    await say(stack, env, text, entities=ents)
    result = stack.llm.tool_results(1)[0]
    assert result.get("is_error") and "unknown place_id 99" in str(result["content"])
    assert [s.text for s in stack.gateway.sent] == ["Noted."]
    assert stack.gateway.reactions == []
