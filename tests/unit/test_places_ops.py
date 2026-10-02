"""Places beyond the live chat (§10.5): dashboard operations on PlaceService, the failed-link
retry, the harvester in other topics and import pre-resolution."""

from __future__ import annotations

import io
import json
import sqlite3
from datetime import timedelta
from typing import Any

from app import inbox_appliers
from app.harvest import Harvester
from app.importer.service import ImportService
from app.llm.client import LLMRequest
from app.settings import set_value
from app.telegram.topics import KEY_ANSWER
from tests.conftest import GROUP_ID, TZ, Env, Stack, make_stack, seed_category
from tests.fakes.fake_batches import FakeBatches
from tests.fakes.fake_llm import FakeLLMClient
from tests.fakes.telegram_export import T0, dinner_chat, dumps, partner, single_chat
from tests.unit.test_harvest import episode, extraction
from tests.unit.test_import_flow import CATEGORIES, NOTES, SINCE, UNTIL
from tests.unit.test_import_flow import extraction as import_extraction
from tests.unit.test_place_flow import ANSWER, FOOD, HOME, REDIRECTS, SHORT, say, stored_texts
from tests.unit.test_place_links import SHARED_Q


async def rows(env: Env, sql: str, *args: Any) -> list[sqlite3.Row]:
    return await env.db.read(lambda c: c.execute(sql, args).fetchall())


async def two_places(env: Env) -> Stack:
    await env.db.write(lambda c: set_value(c, KEY_ANSWER, ANSWER))
    await seed_category(env, "dinner")
    other = "https://www.google.com/maps/place/Keisuke+Ramen/@1.30,103.80,17z"
    stack = make_stack(env, redirects=REDIRECTS)
    await say(stack, env, f"eating here {SHORT}", SHORT)  # place 1, visited
    await say(stack, env, f"also {other}", other)  # place 2, a duplicate under another name
    return stack


async def test_rename_merge_delete(env: Env) -> None:
    stack = await two_places(env)
    p1, p2 = await stack.places.get(1), await stack.places.get(2)
    assert p1 is not None and p2 is not None and p2.note_path == "shared/places/keisuke-ramen.md"
    # Appended without a heading, so it lands inside Details: syncing must keep it.
    await stack.store.write(p2.note_path, mode="append", content="Partner loves the broth.")
    await stack.places.visit(2)
    note2 = await stack.store.read(p2.note_path)
    assert note2 is not None and "Partner loves the broth." in note2.body
    assert "Visits recorded: 1" in note2.body

    renamed = await stack.places.rename(1, "Keisuke Tonkotsu King (Tras)")
    assert renamed.name == "Keisuke Tonkotsu King (Tras)"
    (opt,) = await rows(env, "SELECT name FROM options WHERE place_id = 1")
    assert opt[0] == "Keisuke Tonkotsu King (Tras)"
    note = await stack.store.read(renamed.note_path or "")
    assert note is not None and note.title == "Keisuke Tonkotsu King (Tras)"

    merged = await stack.places.merge(2, 1)
    assert merged.id == 1 and await stack.places.get(2) is None
    note = await stack.store.read(merged.note_path or "")
    assert note is not None and "Partner loves the broth." in note.body
    assert "## From Keisuke Ramen" in note.body and note.body.count("## Details") == 1
    assert not await stack.store.exists("shared/places/keisuke-ramen.md")
    links = await rows(env, "SELECT DISTINCT place_id FROM place_links")
    assert [r[0] for r in links] == [1]

    assert await stack.places.delete(1)
    assert await rows(env, "SELECT * FROM places") == []
    assert (await rows(env, "SELECT place_id FROM decisions"))[0][0] is None
    assert not await stack.store.exists(merged.note_path or "")


async def test_failed_link_is_retried_once_and_patches_history(env: Env) -> None:
    await env.db.write(lambda c: set_value(c, KEY_ANSWER, ANSWER))
    table: dict[str, str] = {}  # Google unreachable at first: 404
    stack = make_stack(env, redirects=table)
    await say(stack, env, f"this one {SHORT} or {HOME}", SHORT, HOME)
    assert await stored_texts(env) == [f"this one {SHORT} or {HOME}"]  # stored as-is

    assert await stack.places.retry_failed() == 0  # too soon
    table.update(REDIRECTS)
    stack.clock.advance(hours=7)
    assert await stack.places.retry_failed() == 2
    (text,) = await stored_texts(env)
    assert f"{SHORT} ⟦place: Keisuke Tonkotsu King" in text
    assert HOME not in text and "⟦location shared⟧" in text  # the home link is dropped
    statuses = await rows(env, "SELECT url, status, attempts FROM place_links ORDER BY url")
    assert [tuple(r) for r in statuses] == [(SHORT, "resolved", 2), (HOME, "unnamed", 2)]

    await env.db.write(lambda c: c.execute("UPDATE place_links SET status = 'failed'"))
    stack.clock.advance(hours=7)
    assert await stack.places.retry_failed() == 0  # one retry only


async def test_harvester_links_observed_decisions_to_places(env: Env) -> None:
    await env.db.write(lambda c: set_value(c, KEY_ANSWER, ANSWER))
    await env.db.write(lambda c: set_value(c, "harvest.min_new_messages", 1))
    await seed_category(env, "dinner")
    option = {"category_phrase": "dinner", "name": "Keisuke Tonkotsu King", "sentiment": 0.8}
    option["tags"] = []
    reply = extraction(episodes=[episode("dinner", "keisuke tonkotsu king")], options=[option])
    stack = make_stack(env, FakeLLMClient(reply), redirects=REDIRECTS)
    inbox_appliers.register(stack.memory, stack.decisions)
    harvester = Harvester(
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
    await say(stack, env, f"eating here {SHORT}", SHORT, topic=FOOD)
    assert stack.gateway.reactions == []  # not the answer topic: the harvester's job

    (run,) = await harvester.tick(force=True)
    assert run.decisions == 1 and run.suggestions == 0  # the option was linked, not suggested
    req = stack.llm.requests[0]
    assert "⟦place: Name" in str(req.system) and "⟦place: Keisuke" in str(req.messages)
    (d,) = await rows(env, "SELECT * FROM decisions")
    assert (d["choice_text"], d["source"], d["place_id"]) == (
        "Keisuke Tonkotsu King",
        "observed",
        1,
    )
    (place,) = await rows(env, "SELECT visit_count FROM places")
    assert place[0] == 1


async def test_import_resolves_links_before_extraction(env: Env) -> None:
    stack = make_stack(env, redirects=REDIRECTS)
    await stack.store.ensure_skeleton(env.users)

    def responder(cid: str, req: LLMRequest) -> str:
        out = json.loads(import_extraction(cid, req))
        if "⟦place: Keisuke" in str(next(iter(req.messages))["content"]):
            out["episodes"][0]["choice"] = "Keisuke Tonkotsu King"
        return json.dumps(out)

    batches = FakeBatches(responder=responder)
    svc = ImportService(
        db=env.db,
        settings=env.settings,
        llm=FakeLLMClient(CATEGORIES, NOTES),
        batches=batches,
        store=stack.store,
        users=env.users,
        tz=TZ,
        imports_dir=env.vault.parent / "imports",
        places=stack.places,
        clock=stack.clock,
    )
    svc.place_interval_s = 0
    last = T0 + timedelta(days=3, minutes=5)
    chat = [
        *dinner_chat(),
        partner(30, [{"type": "plain", "text": "this one "},
                     {"type": "text_link", "text": "here", "href": SHORT}], at=last),
        partner(31, f"or come over {HOME}", at=last + timedelta(minutes=1)),
    ]  # fmt: skip
    job_id = await svc.receive(io.BytesIO(dumps(single_chat(chat))), "result.json")
    job = await svc.job(job_id)
    await svc.configure(
        job_id, chats=job.chats, since=SINCE, until=UNTIL, sender_map=job.sender_map
    )
    await svc.start(job_id, consent=True)
    if svc._kick is not None:
        await svc._kick
    for _ in range(6):
        if (await svc.job(job_id)).status == "review":
            break
        stack.clock.advance(minutes=5)
        await svc.tick()
    assert (await svc.job(job_id)).status == "review"

    (batch,) = batches.submitted.values()
    texts = [str(next(iter(req.messages))["content"]) for _, req in batch]
    last_window = texts[-1]
    assert f"here ({SHORT} ⟦place: Keisuke Tonkotsu King · 1 Tras Link" in last_window
    assert "come over ⟦location shared⟧" in last_window and HOME not in last_window
    assert await rows(env, "SELECT * FROM places") == []  # nothing before review

    from app.importer import jobs

    for kind in ("category", "option", "decision", "note"):
        await svc.edit(lambda c, k=kind: jobs.bulk_approve(c, job_id, k, 0.8))
    summary = await svc.apply(job_id, delete_export=True)
    assert summary["places"] == 1
    (place,) = await rows(env, "SELECT * FROM places")
    assert place["name"] == "Keisuke Tonkotsu King" and place["visit_count"] == 1
    (d,) = await rows(env, "SELECT * FROM decisions WHERE place_id IS NOT NULL")
    assert d["source"] == "import" and d["choice_text"] == "Keisuke Tonkotsu King"
    assert await stack.store.exists(place["note_path"])
    assert SHARED_Q in [r[0] for r in await rows(env, "SELECT final_url FROM place_links")]
