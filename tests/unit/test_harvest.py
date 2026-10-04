"""Memory harvester (§10.4): code-only tick, LLM only when a topic has new chat."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from app import inbox_appliers
from app.db.repos import usage as usage_repo
from app.db.repos.messages import StoredMessage
from app.extraction.schema import EXTRACTION_SCHEMA
from app.harvest import NO_TOPIC, Harvester, windows
from app.llm.client import LLMUnavailable
from app.settings import set_value
from app.telegram.topics import KEY_ANSWER
from app.timeutil import to_sql, utcnow
from tests.conftest import (
    GROUP_ID,
    JACK_TG,
    PARTNER_TG,
    TZ,
    Env,
    Stack,
    make_stack,
    seed_category,
    tg_message,
)
from tests.fakes.fake_llm import FakeLLMClient

ANSWER, FOOD = 5, 9


def extraction(**kw: Any) -> str:
    base: dict[str, Any] = {"episodes": [], "facts": [], "options": [], "skipped_out_of_scope": 0}
    base.update(kw)
    return json.dumps(base)


def fact(
    owner: str, statement: str, conf: float = 0.9, type_: str = "preference"
) -> dict[str, Any]:
    return {
        "owner": owner,
        "type": type_,
        "statement": statement,
        "quote": "q",
        "ts": "",
        "confidence": conf,
    }


def episode(phrase: str, choice: str, outcome: str = "chosen", **kw: Any) -> dict[str, Any]:
    return {
        "summary": f"picking {phrase}",
        "category_phrase": phrase,
        "phrases_seen": kw.get("seen", []),
        "for_users": kw.get("for_users", "both"),
        "options_considered": [],
        "outcome": outcome,
        "choice": choice,
        "ts": kw.get("ts", "2026-10-02T12:00:00+08:00"),
        "quotes": [],
        "confidence": 0.9,
    }


async def setup(env: Env, *replies: Any) -> tuple[Stack, Harvester]:
    await env.db.write(lambda c: set_value(c, KEY_ANSWER, ANSWER))
    # Answer-topic chat must not call Claude here: these tests count harvest requests.
    await env.db.write(lambda c: set_value(c, "telegram.answer_topic_mode", "ambient"))
    stack = make_stack(env, FakeLLMClient(*replies))
    inbox_appliers.register(stack.memory, stack.decisions)
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
        health=stack.health,
        clock=stack.clock,
    )
    await stack.topics.seen(GROUP_ID, FOOD, name="Food")
    return stack, h


async def chat(stack: Stack, env: Env, topic: int | None, *texts: str) -> None:
    for t in texts:
        await stack.adapter.handle_message(
            tg_message(t, from_id=PARTNER_TG, topic=topic, forum=True), env.partner
        )


async def runs(env: Env) -> list[dict[str, Any]]:
    rows = await env.db.read(
        lambda c: c.execute("SELECT * FROM harvest_runs ORDER BY id").fetchall()
    )
    return [dict(r) for r in rows]


# --- windowing -------------------------------------------------------------------------------


def _msg(i: int, minutes: int, text: str = "x") -> StoredMessage:
    from app.timeutil import from_sql

    t = from_sql("2026-10-02 10:00:00") + timedelta(minutes=minutes)
    return StoredMessage(i, GROUP_ID, i, 1, "user", "text", text, to_sql(t))


def test_windows_split_on_gap_and_size() -> None:
    rows = [_msg(1, 0), _msg(2, 30), _msg(3, 200), _msg(4, 201, "y" * 20_000), _msg(5, 202, "z")]
    assert [[r.id for r in w] for w in windows(rows)] == [[1, 2], [3], [4], [5]]


# --- tick --------------------------------------------------------------------------------------


async def test_quiet_group_costs_nothing(env: Env) -> None:
    stack, h = await setup(env)
    await chat(stack, env, ANSWER, "hi")  # answer topic is never harvested
    assert await h.tick() == [] and stack.llm.requests == []
    assert stack.health.last_harvest_tick_at is not None


async def test_no_answer_topic_or_disabled_means_no_harvest(env: Env) -> None:
    stack, h = await setup(env)
    await chat(stack, env, FOOD, *["laksa"] * 6)
    await env.db.write(lambda c: set_value(c, "harvest.enabled", False))
    assert await h.tick() == []
    await env.db.write(lambda c: set_value(c, "harvest.enabled", True))
    await env.db.write(lambda c: set_value(c, KEY_ANSWER, None))
    assert await h.tick(force=True) == [] and stack.llm.requests == []


async def test_interval_is_respected_unless_forced(env: Env) -> None:
    stack, h = await setup(env, extraction(), extraction())
    await chat(stack, env, FOOD, *["laksa"] * 6)
    assert len(await h.tick()) == 1
    await chat(stack, env, FOOD, *["more"] * 6)
    stack.clock.advance(minutes=10)
    assert await h.tick() == []  # 30-min interval
    assert len(await h.tick(force=True)) == 1


async def _stamp(env: Env, when: str) -> None:
    """Rows get SQLite's wall-clock time; pin them to the test clock."""
    await env.db.write(lambda c: c.execute("UPDATE messages SET created_at = ?", (when,)))


async def test_min_new_messages_or_max_age(env: Env) -> None:
    stack, h = await setup(env, extraction())
    await chat(stack, env, FOOD, "just", "three", "msgs")
    await _stamp(env, to_sql(stack.clock()))
    assert await h.tick() == [] and stack.llm.requests == []  # 3 < 5 and fresh
    stack.clock.advance(hours=7)  # the oldest unharvested message is now > 6 h old
    assert [r.status for r in await h.tick(force=True)] == ["done"]


# --- harvest -----------------------------------------------------------------------------------


async def test_preference_in_other_topic_lands_in_inbox(env: Env) -> None:
    """M4 done-when: a preference mentioned in another topic shows up in the memory inbox."""
    await seed_category(env, "dinner", [("Pho", [])])
    stack, h = await setup(
        env,
        extraction(
            facts=[
                fact("partner", "Off seafood this month."),
                fact("partner", "maybe likes jazz", 0.4),
                fact("bob", "Bob hates cats"),
            ],
            episodes=[
                episode("dinner", "pho"),
                episode("which sofa", "grey one"),
                episode("lunch", "", outcome="undecided"),
            ],
            options=[
                {
                    "category_phrase": "dinner",
                    "name": "Ah Hock Laksa",
                    "tags": ["spicy"],
                    "sentiment": 0.8,
                },
                {"category_phrase": "dinner", "name": "Pho", "tags": [], "sentiment": 0.9},
                {"category_phrase": "dinner", "name": "Bad place", "tags": [], "sentiment": -0.5},
            ],
            skipped_out_of_scope=2,
        ),
    )
    await chat(stack, env, ANSWER, "answer-topic chatter")
    await chat(stack, env, FOOD, "I'm off seafood this month", "ok pho then", "ok", "ok", "ok")
    [result] = await h.tick()

    req = stack.llm.requests[0]
    assert (req.purpose, req.model_role, req.json_schema) == (
        "harvest",
        "harvest",
        EXTRACTION_SCHEMA,
    )
    content = str(next(iter(req.messages))["content"])
    assert content.startswith("Topic: Food") and "--- new ---" in content
    assert (
        "partner: I'm off seafood this month" in content and "answer-topic chatter" not in content
    )

    assert (result.facts, result.decisions, result.suggestions, result.skipped_out_of_scope) == (
        1,
        1,
        2,
        2,
    )
    pending = await stack.memory.pending()
    kinds = sorted((i.kind, i.content) for i in pending)
    assert kinds == [
        ("category", "New kind of decision: which sofa (e.g. chose grey one)"),
        ("note", "Off seafood this month."),
        ("option", "New dinner option: Ah Hock Laksa"),
    ]
    note = next(i for i in pending if i.kind == "note")
    assert note.target_path == "memories/partner/preferences.md" and note.source.startswith(
        "topic:Food/msg:"
    )

    observed = await env.db.read(lambda c: c.execute("SELECT * FROM decisions").fetchone())
    assert (
        observed["choice_text"],
        observed["status"],
        observed["source"],
        observed["option_id"],
    ) == (
        "pho",
        "accepted",
        "observed",
        1,
    )
    run = (await runs(env))[0]
    assert (run["thread_id"], run["messages"], run["status"]) == (FOOD, 5, "done")

    # cursor advanced: nothing new → no second LLM call
    assert await h.tick(force=True) == [] and len(stack.llm.requests) == 1


async def test_observed_decision_feeds_recency(env: Env) -> None:
    await seed_category(env, "dinner", [("Pho", []), ("Laksa", [])])
    stack, h = await setup(env, extraction(episodes=[episode("dinner", "Pho", ts=stack_now_iso())]))
    await chat(stack, env, FOOD, *["pho tonight"] * 5)
    await h.tick()
    from app.decisions import categories as cats
    from app.decisions import engine
    from app.decisions.engine import PickRequest

    def _weights(c: sqlite3.Connection) -> dict[str, float]:
        cat = cats.get_by_slug(c, "dinner")
        assert cat is not None
        r = engine.pick(
            c,
            cat,
            PickRequest(cat.id, "both"),
            users_by_slug={"jack": env.jack.id},
            asked_by=env.jack.id,
            chat_id=GROUP_ID,
            now=stack.clock(),
            session_hours=6,
        )
        return r.weights

    w = await env.db.write(_weights)
    assert w["o:1"] < 0.1 < w["o:2"]  # Pho was just eaten (observed), Laksa untouched


def stack_now_iso() -> str:
    from tests.conftest import NOW

    return NOW.astimezone(TZ).isoformat()


async def test_llm_failure_keeps_cursor_for_retry(env: Env) -> None:
    stack, h = await setup(
        env, LLMUnavailable("down"), extraction(facts=[fact("jack", "Likes teh peng.")])
    )
    await chat(stack, env, FOOD, *["teh peng"] * 5)
    assert [r.status for r in await h.tick()] == ["error"]
    assert await stack.memory.pending() == []
    assert [r.status for r in await h.tick(force=True)] == ["done"]
    assert [i.content for i in await stack.memory.pending()] == ["Likes teh peng."]
    assert [r["status"] for r in await runs(env)] == ["error", "done"]


async def test_budget_warning_pauses_harvest(env: Env) -> None:
    await env.db.write(lambda c: set_value(c, "budget.daily_usd", 1.0))
    stack, h = await setup(env)
    row = usage_repo.UsageRow(
        env.jack.id, "chat", GROUP_ID, None, "m", 1, 1, 0, 0, 0.85, to_sql(utcnow())
    )
    await env.db.write(lambda c: usage_repo.insert(c, row))
    await chat(stack, env, FOOD, *["laksa"] * 5)
    assert [r.status for r in await h.tick()] == ["budget"] and stack.llm.requests == []


async def test_pre_topic_rows_are_one_unknown_topic(env: Env) -> None:
    stack, h = await setup(env, extraction())
    for t in ["a", "b", "c", "d", "e"]:  # group without topics → thread_id NULL
        await stack.adapter.handle_message(tg_message(t, from_id=JACK_TG), env.jack)
    [result] = await h.tick()
    assert result.thread_id == NO_TOPIC
    assert "Topic: earlier chat" in str(next(iter(stack.llm.requests[0].messages))["content"])


async def test_approving_suggestions_creates_category_and_option(env: Env) -> None:
    cid = await seed_category(env, "dinner")
    stack, _ = await setup(env)
    cat_item = await stack.memory.suggest(
        kind="category",
        content="New kind: which sofa",
        reason="r",
        source="s",
        payload={
            "phrase": "which sofa",
            "aliases": ["sofa to buy"],
            "description": "Buying a sofa",
        },
    )
    opt_item = await stack.memory.suggest(
        kind="option",
        content="New dinner option: Ah Hock",
        reason="r",
        source="s",
        payload={"category_id": cid, "name": "Ah Hock Laksa", "tags": ["spicy"]},
    )
    await stack.memory.decide(cat_item.id, approve=True, user_id=env.jack.id)
    await stack.memory.decide(opt_item.id, approve=True, user_id=env.jack.id)
    sofa = await stack.decisions.lookup("sofa to buy")
    assert sofa is not None and sofa.slug == "which-sofa" and sofa.description == "Buying a sofa"
    dinner = await stack.decisions.get_category(cid)
    assert dinner is not None
    assert [o.name for o in await stack.decisions.list_options(dinner)] == ["Ah Hock Laksa"]


async def test_inbox_command_shows_suggestions(env: Env) -> None:
    stack, _ = await setup(env)
    await stack.memory.suggest(
        kind="option", content="New dinner option: X", reason="seen", source="s", payload={}
    )
    await stack.adapter.handle_message(tg_message("/inbox", topic=ANSWER, forum=True), env.jack)
    assert stack.gateway.sent[-1].text.startswith("➕ **Suggestion:** New dinner option: X")  # noqa: RUF001


# --- review fixes ------------------------------------------------------------------------------


async def _stamp_each(env: Env, *whens: str) -> None:
    def _up(c: sqlite3.Connection) -> None:
        ids = [r[0] for r in c.execute("SELECT id FROM messages ORDER BY id")]
        for i, when in zip(ids, whens, strict=True):
            c.execute("UPDATE messages SET created_at = ? WHERE id = ?", (when, i))

    await env.db.write(_up)


async def test_ignored_topic_is_never_harvested_even_if_stored_before(env: Env) -> None:
    stack, h = await setup(env, extraction())
    await chat(stack, env, FOOD, *["private stuff"] * 6)
    await env.db.write(lambda c: set_value(c, "telegram.ignored_topic_ids", [FOOD]))
    assert await h.tick(force=True) == [] and stack.llm.requests == []


async def test_naive_episode_time_is_household_local(env: Env) -> None:
    await seed_category(env, "dinner", [("Pho", [])])
    stack, h = await setup(
        env,
        extraction(
            episodes=[
                episode("dinner", "pho", ts="2026-10-02T19:30"),  # SGT, no offset
                episode("dinner", "pho", ts="2030-01-01T12:00:00+08:00"),  # after the chat: bogus
            ]
        ),
    )
    await chat(stack, env, FOOD, *["ok pho"] * 6)
    await _stamp(env, "2026-10-02 13:00:00")  # 21:00 SGT
    await h.tick(force=True)
    rows = await env.db.read(
        lambda c: c.execute(
            "SELECT created_at FROM decisions WHERE source = 'observed' ORDER BY id"
        ).fetchall()
    )
    assert [r[0] for r in rows] == ["2026-10-02 11:30:00", "2026-10-02 13:00:00"]


async def test_concurrent_ticks_harvest_once(env: Env) -> None:
    import asyncio

    stack, h = await setup(
        env,
        extraction(facts=[fact("partner", "Likes laksa.")]),
        extraction(facts=[fact("partner", "Likes laksa.")]),
    )
    await chat(stack, env, FOOD, *["laksa"] * 6)
    complete = stack.llm.complete

    async def slow(req: Any) -> Any:
        await asyncio.sleep(0.02)
        return await complete(req)

    stack.llm.complete = slow  # type: ignore[method-assign]
    await asyncio.gather(h.tick(force=True), h.tick(force=True))  # scheduler + "harvest now"
    assert len(stack.llm.requests) == 1
    assert [i.content for i in await stack.memory.pending()] == ["Likes laksa."]


async def test_failure_after_a_window_keeps_its_counts(env: Env) -> None:
    stack, h = await setup(
        env, extraction(facts=[fact("partner", "Likes laksa.")]), LLMUnavailable("down")
    )
    await chat(stack, env, FOOD, *["laksa"] * 6)
    # Two windows: a > 2 h gap after the third message.
    early, late = "2026-10-02 01:00:00", "2026-10-02 05:00:00"
    await _stamp_each(env, early, early, early, late, late, late)
    [result] = await h.tick(force=True)
    assert (result.status, result.facts) == ("error", 1)
    run = (await runs(env))[0]
    assert (run["status"], run["facts"]) == ("error", 1)


async def test_window_with_repeatedly_invalid_output_is_skipped(env: Env) -> None:
    stack, h = await setup(env, "not json", "[]", "nope", extraction())
    await chat(stack, env, FOOD, *["laksa"] * 6)
    assert [r.status for r in await h.tick(force=True)] == ["error"]
    assert [r.status for r in await h.tick(force=True)] == ["error"]
    assert [r.status for r in await h.tick(force=True)] == ["skipped"]
    assert await h.tick(force=True) == []  # cursor moved past it; no 4th call
    assert len(stack.llm.requests) == 3


async def test_api_errors_are_never_skipped(env: Env) -> None:
    stack, h = await setup(env, *[LLMUnavailable("down")] * 4)
    await chat(stack, env, FOOD, *["laksa"] * 6)
    for _ in range(4):
        assert [r.status for r in await h.tick(force=True)] == ["error"]


async def test_budget_checked_between_windows(env: Env) -> None:
    await env.db.write(lambda c: set_value(c, "budget.daily_usd", 1.0))
    stack, h = await setup(env, extraction(facts=[fact("partner", "Likes laksa.")]))
    complete = stack.llm.complete

    async def costly(req: Any) -> Any:
        row = usage_repo.UsageRow(
            None, "harvest", GROUP_ID, None, "m", 1, 1, 0, 0, 0.9, to_sql(utcnow())
        )
        await env.db.write(lambda c: usage_repo.insert(c, row))
        return await complete(req)

    stack.llm.complete = costly  # type: ignore[method-assign]
    await chat(stack, env, FOOD, *["laksa"] * 6)
    early, late = "2026-10-02 01:00:00", "2026-10-02 05:00:00"
    await _stamp_each(env, early, early, early, late, late, late)
    [result] = await h.tick(force=True)
    assert (result.status, result.facts, len(stack.llm.requests)) == ("budget", 1, 1)
