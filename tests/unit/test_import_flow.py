"""Bootstrap import end to end (§15.2-15.5) with fake batches and a scripted consolidator:
upload → configure → consent → batch extraction → consolidation → review → apply."""

from __future__ import annotations

import io
import json
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pytest

from app.db.repos import usage as usage_repo
from app.importer import jobs
from app.importer.jobs import ImportProblem
from app.importer.service import DuplicateUpload, ImportService
from app.llm.client import LLMRequest, LLMUnavailable, budget_status
from app.settings import set_value
from app.timeutil import to_sql
from tests.conftest import TZ, Env, Stack, make_stack, seed_category
from tests.fakes.fake_batches import FakeBatches
from tests.fakes.fake_llm import FakeLLMClient
from tests.fakes.telegram_export import dinner_chat, dumps, msg, partner, single_chat

SINCE, UNTIL = date(2026, 4, 2), date(2026, 10, 2)


def extraction(cid: str, req: LLMRequest) -> str:
    """One dinner episode per window, a fact about the partner and an option mention."""
    content = str(next(iter(req.messages))["content"])
    day = content.split("[", 1)[1][:10]
    return json.dumps(
        {
            "episodes": [
                {
                    "summary": "Deciding dinner; partner didn't want mala",
                    "category_phrase": "dinner",
                    "phrases_seen": ["makan where"],
                    "for_users": "both",
                    "options_considered": [
                        {"name": "Mala", "by": "jack", "stance": "proposed", "reason": ""},
                        {"name": "mala", "by": "partner", "stance": "rejected",
                         "reason": "too heavy"},
                        {"name": "Yong tau foo", "by": "jack", "stance": "proposed",
                         "reason": ""},
                    ],
                    "outcome": "chosen",
                    "choice": "Yong Tau Foo",
                    "ts": f"{day}T19:03:00",
                    "quotes": ["not mala again lah", "ok ytf"],
                    "confidence": 0.9,
                }
            ],
            "facts": [
                {"owner": "partner", "type": "preference", "statement": "Finds mala too heavy",
                 "quote": "not mala again lah", "ts": f"{day}T19:02:00", "confidence": 0.9},
                {"owner": "mum", "type": "fact", "statement": "Mum likes durian", "quote": "",
                 "ts": "", "confidence": 0.9},
            ],
            "options": [{"category_phrase": "makan where", "name": "yong tau foo",
                         "tags": ["light"], "sentiment": 0.6}],
            "skipped_out_of_scope": 1,
        }
    )  # fmt: skip


CATEGORIES = json.dumps(
    {
        "categories": [
            {"slug": "dinner", "display_name": "Dinner", "description": "What to eat for dinner",
             "recency_tau_days": 3, "default_n": 1, "allow_generated": True,
             "aliases": ["makan where", "晚餐"], "episode_ids": ["E1", "E2", "E3", "E4"]}
        ],
        "unmapped": [],
    }
)  # fmt: skip
NOTES = json.dumps(
    {"notes": [{"target": "profile", "topic": "", "lines": [
        {"text": "Finds mala too heavy; suggest lighter food.", "fact_ids": ["f1", "f2"],
         "confidence": 0.9}]}]}
)  # fmt: skip


@dataclass
class Rig:
    svc: ImportService
    llm: FakeLLMClient
    batches: FakeBatches
    stack: Stack
    env: Env

    async def upload(self, export: dict[str, Any], name: str = "result.json") -> int:
        return await self.svc.receive(io.BytesIO(dumps(export)), name)

    async def configure(self, job_id: int) -> Any:
        job = await self.svc.job(job_id)
        return await self.svc.configure(
            job_id, chats=job.chats, since=SINCE, until=UNTIL, sender_map=job.sender_map
        )

    async def run(self, job_id: int) -> jobs.Job:
        """Start, then tick (5 min apart) until the job leaves the active states."""
        await self.svc.start(job_id, consent=True)
        if self.svc._kick is not None:
            await self.svc._kick
        for _ in range(10):
            job = await self.svc.job(job_id)
            if job.status not in jobs.ACTIVE:
                return job
            self.stack.clock.advance(minutes=5)
            await self.svc.tick()
        return await self.svc.job(job_id)

    async def q(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        return await self.env.db.read(lambda c: c.execute(sql, args).fetchall())


@pytest.fixture
async def rig(env: Env) -> AsyncIterator[Rig]:
    stack = make_stack(env)
    await stack.store.ensure_skeleton(env.users)
    llm = FakeLLMClient(CATEGORIES, NOTES)
    batches = FakeBatches(responder=extraction)
    svc = ImportService(
        db=env.db,
        settings=env.settings,
        llm=llm,
        batches=batches,
        store=stack.store,
        users=env.users,
        tz=TZ,
        imports_dir=env.vault.parent / "imports",
        clock=stack.clock,
    )
    yield Rig(svc, llm, batches, stack, env)


async def test_full_import_reviewed_and_applied(rig: Rig) -> None:
    job_id = await rig.upload(single_chat(dinner_chat()))
    job = await rig.svc.job(job_id)
    assert job.status == "uploaded" and job.msg_count == 12
    assert job.sender_map == {"user111": "jack", "user222": "partner"}  # auto-mapped by id
    assert (job.since, job.until) == ("2026-04-02", "2026-10-02")  # last 6 months by default

    preview = await rig.configure(job_id)
    assert preview.windows == 4 and preview.messages == 12
    assert any("partner: not mala again lah" in line for line in preview.sample)
    assert 0 < preview.est_usd < 1

    with pytest.raises(ImportProblem, match="consent"):
        await rig.svc.start(job_id, consent=False)
    job = await rig.run(job_id)
    assert job.status == "review", job.error
    assert job.consent_at is not None
    assert job.skipped_out_of_scope == 4  # counts only, never content
    (batch,) = rig.batches.submitted.values()
    assert len(batch) == 4
    req = batch[0][1]
    assert req.purpose == "import_extract" and req.import_job_id == job_id
    assert "Never extract" in str(req.system[0]["text"])  # safe-topic rules, layer 1
    assert "makan where" in str(next(iter(req.messages))["content"])

    cons = rig.llm.requests
    assert [r.purpose for r in cons] == ["import_consolidate"] * 2
    assert "out_of_scope" in str(cons[0].system[0]["text"])  # layer 2
    assert "E4 | 2026-07-06 | dinner" in str(next(iter(cons[0].messages))["content"])
    assert "Mum" not in str(next(iter(cons[1].messages))["content"])  # third parties dropped

    cats = await rig.svc.review(job_id, "category")
    opts = {o.payload["name"]: o for o in await rig.svc.review(job_id, "option")}
    decs = await rig.svc.review(job_id, "decision")
    (note,) = await rig.svc.review(job_id, "note")
    assert [c.payload["slug"] for c in cats] == ["dinner"]
    assert set(opts) == {"Mala", "Yong tau foo"}
    assert opts["Mala"].payload["prefs"] == {"jack": 1.12, "partner": 0.68}
    assert opts["Mala"].payload["base_weight"] < 1 < opts["Yong tau foo"].payload["base_weight"]
    assert [d.payload["choice"] for d in decs] == ["Yong tau foo"] * 4
    assert note.payload["path"] == "people/partner.md"
    assert note.payload["lines"][0]["evidence"][0]["quote"] == "not mala again lah"

    for kind in ("category", "option", "decision", "note"):
        await rig.svc.edit(lambda c, k=kind: jobs.bulk_approve(c, job_id, k, 0.8))
    summary = await rig.svc.apply(job_id, delete_export=True)
    assert summary == {"categories": 1, "options": 2, "decisions": 4, "notes": 1}

    cat = await rig.stack.decisions.lookup("makan where")
    assert cat is not None and cat.slug == "dinner"
    assert await rig.stack.decisions.lookup("晚餐") == cat
    (row,) = await rig.q("SELECT created_by FROM categories")
    assert row[0] == "import"
    rows = await rig.q(
        "SELECT choice_text, option_id IS NOT NULL, source, status, created_at FROM decisions "
        "ORDER BY created_at"
    )
    assert tuple(rows[0]) == (
        "Yong tau foo",
        1,
        "import",
        "accepted",
        "2026-07-03 11:03:00",
    )  # original time, UTC
    prefs = await rig.q(
        "SELECT o.name, u.slug, p.multiplier FROM option_prefs p JOIN options o ON o.id = "
        "p.option_id JOIN users u ON u.id = p.user_id ORDER BY 1, 2"
    )
    assert ("Mala", "partner", 0.68) in [tuple(r) for r in prefs]
    profile = await rig.stack.store.read("people/partner.md")
    assert profile is not None and "- Finds mala too heavy" in profile.body
    assert profile.pinned

    job = await rig.svc.job(job_id)
    assert job.status == "done" and job.summary["export_deleted"]
    assert not rig.svc.export_path(job).exists()
    texts = await rig.q("SELECT text, result_json FROM import_windows WHERE job_id = ?", job_id)
    assert all(r[0] is None and r[1] is None for r in texts)  # raw text purged


async def test_duplicate_and_overlapping_exports(rig: Rig) -> None:
    first = await rig.upload(single_chat(dinner_chat(days=2)))
    with pytest.raises(DuplicateUpload) as e:
        await rig.upload(single_chat(dinner_chat(days=2)))
    assert e.value.job_id == first
    await rig.configure(first)
    await rig.run(first)

    later = single_chat([*dinner_chat(days=2), msg(7, "dinner again?"), partner(8, "pasta")])
    second = await rig.upload(later, "later.json")
    preview = await rig.configure(second)
    assert preview.skipped_duplicates == 6 and preview.messages == 2


async def test_cancel_stops_the_batch_and_a_reupload_reopens(rig: Rig) -> None:
    rig.batches.polls_until_ended = 99
    export = single_chat(dinner_chat())
    job_id = await rig.upload(export)
    await rig.configure(job_id)
    job = await rig.run(job_id)
    assert job.status == "extracting"
    await rig.svc.cancel(job_id)
    assert rig.batches.cancelled == ["msgbatch_1"]
    assert (await rig.svc.job(job_id)).status == "cancelled"
    assert await rig.upload(export) == job_id
    job = await rig.svc.job(job_id)
    assert job.status == "uploaded" and job.window_count == 0


async def test_failed_windows_retry_then_give_up(rig: Rig) -> None:
    def flaky(cid: str, req: LLMRequest) -> str | Exception:
        content = str(next(iter(req.messages))["content"])
        return RuntimeError("boom") if "2026-07-04" in content else extraction(cid, req)

    rig.batches.responder = flaky
    rig.llm = FakeLLMClient()
    job_id = await rig.upload(single_chat(dinner_chat()))
    await rig.configure(job_id)
    rig.svc._llm = FakeLLMClient(
        CATEGORIES.replace('"E1", "E2", "E3", "E4"', '"E1", "E2", "E3"'), NOTES
    )
    job = await rig.run(job_id)
    assert job.status == "review"
    counts = await rig.env.db.read(lambda c: jobs.window_counts(c, job_id))
    assert counts == {"done": 3, "failed": 1}
    assert len(rig.batches.submitted) == 3  # first try + 2 retries of the bad window


async def test_consolidation_retries_without_paying_twice(rig: Rig) -> None:
    rig.svc._llm = llm = FakeLLMClient(CATEGORIES, LLMUnavailable("overloaded"), NOTES)
    job_id = await rig.upload(single_chat(dinner_chat()))
    await rig.configure(job_id)
    job = await rig.run(job_id)
    assert job.status == "review"
    assert len(llm.requests) == 3  # categories once, notes twice
    assert "categories" in job.consolidation


async def test_budget_guard_and_daily_cap_exclusion(rig: Rig) -> None:
    job_id = await rig.upload(single_chat(dinner_chat()))
    await rig.configure(job_id)
    await rig.env.db.write(lambda c: set_value(c, "budget.monthly_usd", 0.0001))
    with pytest.raises(ImportProblem, match="monthly_usd"):
        await rig.svc.start(job_id, consent=True)
    await rig.env.db.write(lambda c: set_value(c, "budget.monthly_usd", 15.0))

    row = usage_repo.UsageRow(None, "import_extract", None, job_id, "m", 1, 1, 0, 0, 2.5,
                              to_sql(rig.stack.clock()))  # fmt: skip
    await rig.env.db.write(lambda c: usage_repo.insert(c, row))
    status = await budget_status(rig.env.db, await rig.env.settings.load(), TZ)
    assert status.daily_spent == 0 and status.monthly_spent >= 2.5


async def test_review_edits_merges_and_rejections(rig: Rig) -> None:
    two = json.dumps(
        {"categories": [
            {"slug": "dinner", "display_name": "Dinner", "description": "", "recency_tau_days": 3,
             "default_n": 1, "allow_generated": True, "aliases": ["makan where"],
             "episode_ids": ["E1", "E2", "E3"]},
            {"slug": "supper", "display_name": "Supper", "description": "", "recency_tau_days": 3,
             "default_n": 1, "allow_generated": True, "aliases": ["supper"],
             "episode_ids": ["E4"]}],
         "unmapped": []})  # fmt: skip
    await seed_category(rig.env, "supper")  # exists → a single episode is enough
    rig.svc._llm = FakeLLMClient(two, NOTES)
    job_id = await rig.upload(single_chat(dinner_chat()))
    await rig.configure(job_id)
    await rig.run(job_id)
    dinner, supper = await rig.svc.review(job_id, "category")
    assert supper.payload["existing_id"] is not None

    await rig.svc.edit(lambda c: jobs.merge_categories(c, job_id, supper.id, dinner.id))
    await rig.svc.edit(
        lambda c: jobs.edit_category(
            c, job_id, dinner.id, slug="Evening Meal", display_name="Evening meal",
            description="what to eat", tau=500, default_n=2, aliases=["Makan where", "dinner"],
        )
    )  # fmt: skip
    cats = {c.id: c for c in await rig.svc.review(job_id, "category")}
    assert cats[supper.id].status == "merged"
    assert cats[dinner.id].payload["recency_tau_days"] == 365
    decs = await rig.svc.review(job_id, "decision")
    assert {d.category_item_id for d in decs} == {dinner.id}
    assert {d.payload["category_slug"] for d in decs} == {"evening-meal"}

    with pytest.raises(ImportProblem, match="already merged"):
        await rig.svc.edit(lambda c: jobs.merge_categories(c, job_id, supper.id, dinner.id))

    # Rejecting the category drops its options and decisions at apply.
    await rig.svc.edit(lambda c: jobs.decide(c, job_id, dinner.id, approve=False))
    for kind in ("option", "decision"):
        await rig.svc.edit(lambda c, k=kind: jobs.bulk_approve(c, job_id, k, 0.0))
    summary = await rig.svc.apply(job_id, delete_export=False)
    assert summary["categories"] == summary["options"] == summary["decisions"] == 0
    assert rig.svc.export_path(await rig.svc.job(job_id)).exists()


async def test_apply_into_existing_category_keeps_live_prefs(rig: Rig) -> None:
    cid = await seed_category(rig.env, "dinner", [("Mala", ["spicy"])])
    jack = rig.env.jack.id
    await rig.env.db.write(
        lambda c: c.execute(
            "INSERT INTO option_prefs(option_id, user_id, multiplier) "
            "SELECT id, ?, 2.0 FROM options WHERE name = 'Mala'",
            (jack,),
        )
    )
    job_id = await rig.upload(single_chat(dinner_chat()))
    await rig.configure(job_id)
    await rig.run(job_id)
    (cat,) = await rig.svc.review(job_id, "category")
    assert cat.payload["existing_id"] == cid
    for kind in ("category", "option", "decision"):
        await rig.svc.edit(lambda c, k=kind: jobs.bulk_approve(c, job_id, k, 0.0))
    summary = await rig.svc.apply(job_id, delete_export=True)
    assert summary["categories"] == 0 and summary["options"] == 1  # YTF new, Mala existed
    prefs = await rig.q(
        "SELECT u.slug, p.multiplier FROM option_prefs p JOIN options o ON o.id = p.option_id "
        "JOIN users u ON u.id = p.user_id WHERE o.name = 'Mala' ORDER BY 1"
    )
    assert [tuple(r) for r in prefs] == [("jack", 2.0), ("partner", 0.68)]
    assert await rig.stack.decisions.lookup("makan where") is not None


async def test_unmapped_episode_can_be_assigned(rig: Rig) -> None:
    rig.svc._llm = FakeLLMClient(
        CATEGORIES.replace('"E1", "E2", "E3", "E4"', '"E1", "E2", "E3"'), NOTES
    )
    job_id = await rig.upload(single_chat(dinner_chat()))
    await rig.configure(job_id)
    await rig.run(job_id)
    (un,) = await rig.svc.review(job_id, "unmapped")
    assert un.payload["why"] == "not assigned to a category"
    (cat,) = await rig.svc.review(job_id, "category")
    assert len(await rig.svc.review(job_id, "decision")) == 3
    await rig.svc.edit(lambda c: jobs.assign_unmapped(c, job_id, un.id, cat.id))
    assert len(await rig.svc.review(job_id, "decision")) == 4
    assert (await rig.svc.review(job_id, "unmapped"))[0].status == "assigned"


async def test_configure_validation(rig: Rig) -> None:
    job_id = await rig.upload(single_chat(dinner_chat()))
    job = await rig.svc.job(job_id)
    with pytest.raises(ImportProblem, match="at least one chat"):
        await rig.svc.configure(job_id, chats=["nope"], since=SINCE, until=UNTIL, sender_map={})
    with pytest.raises(ImportProblem, match="no new messages"):
        await rig.svc.configure(
            job_id, chats=job.chats, since=UNTIL - timedelta(days=1), until=UNTIL,
            sender_map=job.sender_map,
        )  # fmt: skip
    await rig.svc.configure(
        job_id, chats=job.chats, since=SINCE, until=UNTIL, sender_map={"user111": "admin"}
    )
    assert (await rig.svc.job(job_id)).sender_map == {"user111": "other"}
