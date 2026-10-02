"""Bootstrap import (§15): one job per uploaded Telegram export, driven by the dashboard wizard
(§15.2) and the scheduler.

  uploaded ──configure──► configured ──start (consent)──► extracting ──► consolidating ──► review
      ▲                                                     │ batch per        │ Opus calls,     │
      └──────── reconfigure / reopen a cancelled job ───────┘ pending windows  │ items written   │
                                                                                apply ─► done ◄─┘

``tick()`` runs every minute: it submits pending windows as one Message Batch, polls batches
every ``import.poll_min`` minutes, and runs consolidation when every window is finished. Each
window has its own status, so a crash or restart only resubmits what's unfinished (§15.5).
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import secrets
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import fmean
from typing import Any, BinaryIO
from zoneinfo import ZoneInfo

from anthropic.types import MessageParam
from pydantic import ValidationError

from app.brain.store import NoteStore
from app.db.database import Database
from app.db.repos.users import UserRecord
from app.decisions import categories as cats
from app.extraction.schema import EXTRACTION_SCHEMA, Extraction, OptionSeen
from app.importer import consolidate as cs
from app.importer import jobs
from app.importer import prompts as pr
from app.importer import telegram as tg
from app.importer.jobs import ImportProblem, Job
from app.importer.windowing import Window, build_windows, covered, estimate_cost, estimate_tokens
from app.llm.client import (
    BATCH_DISCOUNT,
    CONSOLIDATION_TIMEOUT_S,
    BatchClient,
    LLMClient,
    LLMError,
    LLMRequest,
    budget_status,
)
from app.settings import SettingsStore
from app.timeutil import from_sql, to_sql, utcnow

log = logging.getLogger(__name__)

EXTRACT_MAX_TOKENS = 16_000  # Opus-tier thinks first; thinking counts against max_tokens
CONSOLIDATE_MAX_TOKENS = 32_000
MAX_WINDOW_ATTEMPTS = 3
MAX_CONSOLIDATE_ATTEMPTS = 3
BATCH_LIMIT = 10_000
SAMPLE_LINES = 12


class DuplicateUpload(ImportProblem):
    def __init__(self, job_id: int) -> None:
        super().__init__(f"this export was already uploaded (import #{job_id})")
        self.job_id = job_id


@dataclass(frozen=True)
class Preview:
    windows: int
    messages: int
    skipped_duplicates: int
    input_tokens: int
    est_usd: float
    extract_usd: float
    consolidate_usd: float
    monthly_left: float
    sample: list[str]


@dataclass(frozen=True)
class Progress:
    windows: dict[str, int]
    spent_usd: float


class ImportService:
    def __init__(
        self,
        *,
        db: Database,
        settings: SettingsStore,
        llm: LLMClient,
        batches: BatchClient,
        store: NoteStore,
        users: Sequence[UserRecord],
        tz: ZoneInfo,
        imports_dir: Path,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._settings = settings
        self._llm = llm
        self._batches = batches
        self._store = store
        self._users = list(users)
        self._slugs = [u.slug for u in users]
        self._tz = tz
        self.imports_dir = imports_dir
        self._clock = clock
        self._lock = asyncio.Lock()
        self._kick: asyncio.Task[None] | None = None

    # --- ① upload, ② validate ----------------------------------------------------------------

    def sweep(self) -> None:
        """Startup: drop half-written uploads left by a crash."""
        for part in self.imports_dir.glob("upload-*.part"):
            part.unlink(missing_ok=True)

    def export_path(self, job: Job) -> Path:
        return self.imports_dir / f"{job.file_sha256}.json"

    async def receive(self, src: BinaryIO, filename: str) -> int:
        """Stream an upload to disk, dedupe by sha256, unpack a zip, scan it, create the job.
        Re-uploading the file of a cancelled/failed job reopens that job."""
        s = await self._settings.load()
        limit = s.import_max_upload_mb * 1024 * 1024
        self.imports_dir.mkdir(parents=True, exist_ok=True)
        part = self.imports_dir / f"upload-{secrets.token_hex(8)}.part"
        try:
            await asyncio.to_thread(tg.copy_limited, src, part, limit)
            sha = await asyncio.to_thread(tg.sha256_of, part)
            prior = await self._db.read(lambda c: jobs.by_sha(c, sha))
            if prior is not None and prior.status not in jobs.REOPENABLE:
                raise DuplicateUpload(prior.id)
            dest = self.imports_dir / f"{sha}.json"
            if await asyncio.to_thread(tg.is_zip, part):
                await asyncio.to_thread(tg.extract_result_json, part, dest)
            else:
                part.replace(dest)
            try:
                summary = await asyncio.to_thread(tg.scan, dest, self._tz)
            except tg.ExportError:
                dest.unlink(missing_ok=True)
                raise
        finally:
            part.unlink(missing_ok=True)
        return await self._db.write(lambda c: self._create(c, sha, filename, summary, prior))

    def _create(
        self,
        c: sqlite3.Connection,
        sha: str,
        filename: str,
        summary: tg.ExportSummary,
        prior: Job | None,
    ) -> int:
        today = self._clock().astimezone(self._tz).date()
        since = (today - timedelta(days=183)).isoformat()
        chats = [summary.chats[0].ref] if summary.format == "single" else []
        by_tg = {f"user{u.telegram_id}": u.slug for u in self._users}
        senders = {ref for ch in summary.chats for ref in ch.senders}
        sender_map = {ref: by_tg.get(ref, "other") for ref in sorted(senders)}
        fields: dict[str, Any] = dict(
            source="telegram",
            status="uploaded",
            since=since,
            until=today.isoformat(),
            msg_count=summary.messages,
            window_count=0,
            filename=filename[:200],
            format=summary.format,
            meta_json=json.dumps(summary.to_json(), ensure_ascii=False),
            chats_json=json.dumps(chats),
            sender_map_json=json.dumps(sender_map),
            consent_at=None,
            est_cost_usd=None,
            error=None,
            attempts=0,
            consolidation_json=None,
        )
        if prior is not None:
            c.execute("DELETE FROM import_windows WHERE job_id = ?", (prior.id,))
            c.execute("DELETE FROM import_items WHERE job_id = ?", (prior.id,))
            jobs.update(c, prior.id, **fields)
            log.info("import reopened", extra={"job_id": prior.id})
            return prior.id
        cols = ", ".join(["file_sha256", *fields])
        cur = c.execute(
            f"INSERT INTO import_jobs({cols}) VALUES ({','.join('?' * (len(fields) + 1))})",
            (sha, *fields.values()),
        )
        job_id = int(cur.lastrowid or 0)
        log.info("import uploaded", extra={"job_id": job_id, "messages": summary.messages})
        return job_id

    async def job(self, job_id: int) -> Job:
        job = await self._db.read(lambda c: jobs.get(c, job_id))
        if job is None:
            raise ImportProblem("no such import")
        return job

    # --- ③ chats & range, ④ senders, ⑤ preview -----------------------------------------------

    async def configure(
        self,
        job_id: int,
        *,
        chats: Sequence[str],
        since: date,
        until: date,
        sender_map: dict[str, str],
    ) -> Preview:
        job = await self.job(job_id)
        if job.status not in jobs.EDITABLE:
            raise ImportProblem("this import can no longer be changed")
        known = set(job.chat_names)
        selected = [c for c in chats if c in known]
        if not selected:
            raise ImportProblem("choose at least one chat")
        if since > until:
            raise ImportProblem("the start date is after the end date")
        allowed = {*self._slugs, "other"}
        senders = {k: (v if v in allowed else "other") for k, v in sender_map.items()}
        await self._db.write(
            lambda c: jobs.update(
                c,
                job_id,
                chats_json=json.dumps(selected),
                since=since.isoformat(),
                until=until.isoformat(),
                sender_map_json=json.dumps(senders),
                status="uploaded",
            )
        )
        return await self._prepare(await self.job(job_id))

    def _windows(self, job: Job, ranges: dict[str, list[tuple[int, int]]]) -> tuple[
        list[Window], int, int
    ]:  # fmt: skip
        """Thread: parse the export again (streaming), filter, and window it."""
        since, until = date.fromisoformat(job.since), date.fromisoformat(job.until)
        kept = dupes = 0

        def selected() -> Any:
            nonlocal kept, dupes
            for msg in tg.parse(self.export_path(job), self._tz, set(job.chats)):
                if not since <= msg.ts.astimezone(self._tz).date() <= until:
                    continue
                if covered(msg, ranges):
                    dupes += 1
                    continue
                kept += 1
                yield msg

        windows = list(build_windows(selected(), job.sender_map, self._tz))
        return windows, kept, dupes

    async def _prepare(self, job: Job) -> Preview:
        ranges = await self._db.read(lambda c: jobs.covered_ranges(c, job.id))
        windows, kept, dupes = await asyncio.to_thread(self._windows, job, ranges)
        if not windows:
            raise ImportProblem("no new messages in that range (already imported, or empty)")
        s = await self._settings.load()
        system_tokens = estimate_tokens(self._extract_system())
        est = estimate_cost(
            [w.tokens for w in windows],
            system_tokens,
            s.pricing.get(s.model_for("import_extract")),
            s.pricing.get(s.model_for("import_consolidate")),
            BATCH_DISCOUNT,
        )

        def _save(c: sqlite3.Connection) -> None:
            c.execute("DELETE FROM import_windows WHERE job_id = ?", (job.id,))
            c.executemany(
                "INSERT INTO import_windows(job_id, chat_ref, start_ts, end_ts, text, status, ord, "
                "first_msg_id, last_msg_id, msg_count, est_tokens) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
                [
                    (
                        job.id,
                        w.chat_ref,
                        to_sql(w.start),
                        to_sql(w.end),
                        w.text,
                        w.ord,
                        w.first_msg_id,
                        w.last_msg_id,
                        w.msg_count,
                        w.tokens,
                    )
                    for w in windows
                ],
            )
            jobs.update(
                c,
                job.id,
                window_count=len(windows),
                est_cost_usd=round(est.total_usd, 4),
                meta_json=json.dumps(
                    {**job.meta, "selected_messages": kept, "skipped_duplicates": dupes},
                    ensure_ascii=False,
                ),
                status="configured",
            )

        await self._db.write(_save)
        log.info(
            "import windowed",
            extra={"job_id": job.id, "windows": len(windows), "est_usd": round(est.total_usd, 2)},
        )
        return await self.preview(job.id)

    async def preview(self, job_id: int) -> Preview:
        job = await self.job(job_id)
        rows = await self._db.read(
            lambda c: c.execute(
                "SELECT text, est_tokens FROM import_windows WHERE job_id = ? ORDER BY ord",
                (job_id,),
            ).fetchall()
        )
        s = await self._settings.load()
        status = await budget_status(self._db, s, self._tz)
        sample: list[str] = []
        for r in rows[:: max(len(rows) // 3, 1)][:3]:
            sample += (r["text"] or "").splitlines()[: SAMPLE_LINES // 3]
        system_tokens = estimate_tokens(self._extract_system())
        est = estimate_cost(
            [r["est_tokens"] for r in rows],
            system_tokens,
            s.pricing.get(s.model_for("import_extract")),
            s.pricing.get(s.model_for("import_consolidate")),
            BATCH_DISCOUNT,
        )
        return Preview(
            windows=len(rows),
            messages=int(job.meta.get("selected_messages", 0)),
            skipped_duplicates=int(job.meta.get("skipped_duplicates", 0)),
            input_tokens=est.input_tokens,
            est_usd=est.total_usd,
            extract_usd=est.extract_usd,
            consolidate_usd=est.consolidate_usd,
            monthly_left=max(status.monthly_cap - status.monthly_spent, 0.0),
            sample=sample,
        )

    # --- ⑥ run --------------------------------------------------------------------------------

    async def start(self, job_id: int, *, consent: bool) -> None:
        if not consent:
            raise ImportProblem("tick the consent box: both of you must agree first")
        if not self._llm.configured:
            raise ImportProblem("ANTHROPIC_API_KEY isn't set (or was rejected)")
        p = await self.preview(job_id)
        if p.est_usd > p.monthly_left:
            raise ImportProblem(
                f"the estimate (${p.est_usd:.2f}) is more than what's left of this month's "
                f"budget (${p.monthly_left:.2f}); raise budget.monthly_usd in Settings first"
            )
        now = to_sql(self._clock())

        def _go(c: sqlite3.Connection) -> None:
            jobs.set_status(c, job_id, "extracting", expect=("configured",))
            jobs.update(c, job_id, consent_at=now, error=None, polled_at=None)

        await self._db.write(_go)
        log.info("import started", extra={"job_id": job_id})
        self.kick()

    def kick(self) -> None:
        """Advance right away instead of waiting for the next scheduler minute."""
        if self._kick is None or self._kick.done():
            self._kick = asyncio.create_task(self.tick())

    async def cancel(self, job_id: int) -> None:
        job = await self.job(job_id)
        if job.status in ("done", "cancelled", "applying"):
            raise ImportProblem(f"can't cancel an import that is {job.status}")
        batch_ids = await self._db.read(
            lambda c: [
                r[0]
                for r in c.execute(
                    "SELECT DISTINCT batch_id FROM import_windows WHERE job_id = ? "
                    "AND status = 'submitted' AND batch_id IS NOT NULL",
                    (job_id,),
                )
            ]
        )
        await self._db.write(
            lambda c: jobs.set_status(
                c, job_id, "cancelled", expect=("uploaded", "configured", *jobs.ACTIVE, "review")
            )
        )
        for batch_id in batch_ids:
            await self._batches.cancel_batch(batch_id)
        log.info("import cancelled", extra={"job_id": job_id})

    async def progress(self, job_id: int) -> Progress:
        def _q(c: sqlite3.Connection) -> Progress:
            return Progress(jobs.window_counts(c, job_id), jobs.spent(c, job_id))

        return await self._db.read(_q)

    async def tick(self) -> None:
        async with self._lock:
            for job in await self._db.read(lambda c: jobs.with_status(c, *jobs.ACTIVE[:2])):
                try:
                    if job.status == "extracting":
                        await self._extract(job)
                    else:
                        await self._consolidate(job)
                except Exception as e:
                    log.exception("import step failed", extra={"job_id": job.id})
                    await self._db.write(
                        functools.partial(jobs.update, job_id=job.id, error=_describe(e))
                    )

    async def _due(self, job: Job) -> bool:
        if job.polled_at is None:
            return True
        s = await self._settings.load()
        return self._clock() - from_sql(job.polled_at) >= timedelta(minutes=s.import_poll_min)

    async def _touch(self, job_id: int, **fields: Any) -> None:
        now = to_sql(self._clock())
        await self._db.write(lambda c: jobs.update(c, job_id, polled_at=now, **fields))

    def _extract_system(self) -> str:
        return pr.extract_system(", ".join(f"{u.slug} ({u.display_name})" for u in self._users))

    def _template(self, job_id: int) -> LLMRequest:
        return LLMRequest(
            purpose="import_extract",
            model_role="import_extract",
            system=[
                {
                    "type": "text",
                    "text": self._extract_system(),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[],
            max_tokens=EXTRACT_MAX_TOKENS,
            json_schema=EXTRACTION_SCHEMA,
            import_job_id=job_id,
        )

    async def _extract(self, job: Job) -> None:
        rows = await self._db.read(
            lambda c: c.execute(
                "SELECT id, chat_ref, text, status, batch_id, attempts FROM import_windows "
                "WHERE job_id = ? AND status IN ('pending', 'submitted') ORDER BY ord",
                (job.id,),
            ).fetchall()
        )
        submitted = {r["batch_id"] for r in rows if r["status"] == "submitted"}
        pending = [r for r in rows if r["status"] == "pending"]
        if not rows:
            await self._finish_extraction(job)
            return
        if not await self._due(job):
            return
        template = self._template(job.id)
        for batch_id in sorted(b for b in submitted if b):
            if await self._batches.batch_status(batch_id) == "ended":
                results = await self._batches.batch_results(batch_id, template)
                await self._collect(job, batch_id, results)
        if pending and not submitted:
            names = job.chat_names
            for start in range(0, len(pending), BATCH_LIMIT):
                chunk = pending[start : start + BATCH_LIMIT]
                items = []
                for r in chunk:
                    chat = names.get(r["chat_ref"], "chat")
                    content = f"Chat: {chat}\n\n{r['text']}"
                    msg: MessageParam = {"role": "user", "content": content}
                    items.append((f"w{r['id']}", replace(template, messages=[msg])))
                batch_id = await self._batches.submit_batch(items)
                ids = [r["id"] for r in chunk]
                await self._db.write(functools.partial(_mark_submitted, batch_id=batch_id, ids=ids))
        await self._touch(job.id, error=None)
        counts = await self._db.read(lambda c: jobs.window_counts(c, job.id))
        if not counts.get("pending") and not counts.get("submitted"):
            await self._finish_extraction(job)

    async def _collect(self, job: Job, batch_id: str, results: Sequence[Any]) -> None:
        by_id = {r.custom_id: r for r in results}
        rows = await self._db.read(
            lambda c: c.execute(
                "SELECT id, attempts FROM import_windows "
                "WHERE batch_id = ? AND status = 'submitted'",
                (batch_id,),
            ).fetchall()
        )
        done: list[tuple[str, int, int]] = []
        retry: list[tuple[str, int]] = []
        for r in rows:
            res = by_id.get(f"w{r['id']}")
            extraction: Extraction | None = None
            if res is not None and res.response is not None:
                try:
                    extraction = Extraction.model_validate(json.loads(res.response.text))
                except (json.JSONDecodeError, ValidationError):
                    extraction = None
            if extraction is not None:
                skipped = extraction.skipped_out_of_scope
                done.append((extraction.model_dump_json(), skipped, r["id"]))
            else:
                status = "pending" if r["attempts"] < MAX_WINDOW_ATTEMPTS else "failed"
                retry.append((status, r["id"]))

        def _save(c: sqlite3.Connection) -> None:
            c.executemany(
                "UPDATE import_windows SET status = 'done', result_json = ?, "
                "skipped_out_of_scope = ? WHERE id = ?",
                done,
            )
            c.executemany("UPDATE import_windows SET status = ? WHERE id = ?", retry)
            jobs.update(c, job.id, cost_usd=jobs.spent(c, job.id))

        await self._db.write(_save)
        log.info(
            "import batch collected",
            extra={"job_id": job.id, "done": len(done), "retry_or_failed": len(retry)},
        )

    async def _finish_extraction(self, job: Job) -> None:
        counts = await self._db.read(lambda c: jobs.window_counts(c, job.id))
        if not counts.get("done"):
            await self._db.write(
                lambda c: jobs.update(
                    c, job.id, status="failed", error="no window could be extracted"
                )
            )
            return
        await self._db.write(
            lambda c: jobs.update(c, job.id, status="consolidating", polled_at=None, attempts=0)
        )
        log.info("import extraction finished", extra={"job_id": job.id, **counts})
        await self._consolidate(await self.job(job.id))

    # --- consolidation ---------------------------------------------------------------------

    async def _ask(self, job: Job, system: str, user: str, schema: dict[str, Any]) -> Any:
        resp = await self._llm.complete(
            LLMRequest(
                purpose="import_consolidate",
                model_role="import_consolidate",
                system=[{"type": "text", "text": system}],
                messages=[{"role": "user", "content": user}],
                max_tokens=CONSOLIDATE_MAX_TOKENS,
                json_schema=schema,
                import_job_id=job.id,
                timeout_s=CONSOLIDATION_TIMEOUT_S,
                stream=True,
            )
        )
        return json.loads(resp.text)

    async def _cached(
        self, job: Job, key: str, system: str, user: str, schema: dict[str, Any]
    ) -> Any:
        """Each consolidation call's result is kept on the job, so a later failure doesn't pay
        for it again."""
        if key in job.consolidation:
            return job.consolidation[key]
        result = await self._ask(job, system, user, schema)
        job.consolidation[key] = result
        blob = json.dumps(job.consolidation, ensure_ascii=False)
        await self._db.write(lambda c: jobs.update(c, job.id, consolidation_json=blob))
        return result

    async def _consolidate(self, job: Job) -> None:
        if not await self._due(job):
            return
        if job.attempts >= MAX_CONSOLIDATE_ATTEMPTS:
            await self._db.write(lambda c: jobs.update(c, job.id, status="failed"))
            return
        await self._touch(job.id, attempts=job.attempts + 1)
        try:
            await self._run_consolidation(job)
        except (LLMError, json.JSONDecodeError, ValidationError, KeyError, TypeError) as e:
            error = _describe(e)
            log.warning("import consolidation failed", extra={"job_id": job.id, "error": error})
            await self._db.write(lambda c: jobs.update(c, job.id, error=error))

    async def _run_consolidation(self, job: Job) -> None:
        rows = await self._db.read(
            lambda c: c.execute(
                "SELECT ord, end_ts, result_json, skipped_out_of_scope FROM import_windows "
                "WHERE job_id = ? AND status = 'done' ORDER BY ord",
                (job.id,),
            ).fetchall()
        )
        episodes: list[cs.EpisodeRec] = []
        facts: list[cs.FactRec] = []
        seen: list[tuple[OptionSeen, datetime]] = []
        skipped = 0
        for r in rows:
            ex = Extraction.model_validate_json(r["result_json"])
            end = from_sql(r["end_ts"])
            skipped += r["skipped_out_of_scope"]
            for i, ep in enumerate(ex.episodes, 1):
                episodes.append(
                    cs.EpisodeRec(f"w{r['ord']}-e{i}", ep, cs.parse_ts(ep.ts, end, self._tz))
                )
            for f in ex.facts:
                facts.append(cs.FactRec(f"f{len(facts) + 1}", f, cs.parse_ts(f.ts, end, self._tz)))
            seen += [(o, end) for o in ex.options]
        episodes = cs.dedupe_episodes(episodes)

        existing, aliases, options = await self._db.read(_catalog)
        short = {f"E{i}": rec for i, rec in enumerate(episodes, 1)}
        if short:
            ex_lines = "\n".join(f"{c.slug} | {c.display_name} | {c.description}" for c in existing)
            ep_lines = "\n".join(cs.episode_line(k, v, self._tz) for k, v in short.items())
            raw = await self._cached(
                job,
                "categories",
                pr.categories_system(),
                f"Existing categories (slug | name | description):\n{ex_lines or '(none)'}\n\n"
                f"Episodes (id | date | kind | phrasings | summary | options | outcome):\n"
                f"{ep_lines}",
                pr.CATEGORIES_SCHEMA,
            )
            design = cs.validate_design(raw, short, existing, aliases)
        else:
            design = cs.Design([], [])

        opts = cs.build_options(design, episodes, seen, self._slugs, options, self._tz)
        owners = [*self._slugs, "shared"]
        kept = cs.filter_facts(facts, owners)
        notes: list[cs.NoteProposal] = []
        names = {u.slug: u.display_name for u in self._users}
        for owner in owners:
            mine = [f for f in kept if f.fact.owner == owner]
            if not mine:
                continue
            who = "both of them (the household)" if owner == "shared" else names[owner]
            raw_notes = await self._cached(
                job,
                f"notes:{owner}",
                pr.notes_system(who),
                "Facts (id | date | type | statement | quote | confidence):\n"
                + "\n".join(cs.fact_line(f, self._tz) for f in mine),
                pr.NOTES_SCHEMA,
            )
            notes += cs.validate_notes(raw_notes, owner, {f.id: f for f in mine}, self._tz)

        by_id = {e.id: e for e in episodes}
        await self._db.write(
            lambda c: self._write_items(c, job, design, opts, notes, by_id, skipped)
        )
        log.info(
            "import consolidated",
            extra={
                "job_id": job.id,
                "categories": len(design.categories),
                "options": len(opts),
                "notes": len(notes),
                "episodes": len(episodes),
            },
        )

    def _write_items(
        self,
        c: sqlite3.Connection,
        job: Job,
        design: cs.Design,
        opts: Sequence[cs.OptionProposal],
        notes: Sequence[cs.NoteProposal],
        episodes: dict[str, cs.EpisodeRec],
        skipped: int,
    ) -> None:
        if (jobs.get(c, job.id) or job).status != "consolidating":
            return  # cancelled while the calls were running
        c.execute("DELETE FROM import_items WHERE job_id = ?", (job.id,))

        def ev(rec: cs.EpisodeRec) -> dict[str, Any]:
            return {
                "date": f"{rec.ts.astimezone(self._tz):%Y-%m-%d}",
                "summary": rec.ep.summary,
                "quotes": rec.ep.quotes,
            }

        cat_ids: dict[str, int] = {}
        for cat in design.categories:
            recs = [episodes[e] for e in cat.episode_ids]
            conf = fmean(r.ep.confidence for r in recs) if recs else 1.0
            cat_ids[cat.slug] = jobs.insert_item(
                c,
                job.id,
                "category",
                cat.payload(),
                [ev(r) for r in recs[:5]],
                conf,
                ref=cat.slug,
            )
        for opt in opts:
            jobs.insert_item(
                c,
                job.id,
                "option",
                opt.payload(),
                opt.evidence,
                opt.confidence,
                category_item_id=cat_ids[opt.category_slug],
            )
        category_of = design.category_of()
        for rec in episodes.values():
            slug = category_of.get(rec.id)
            if slug is None or not rec.chosen:
                continue
            jobs.insert_item(
                c,
                job.id,
                "decision",
                {
                    "category_slug": slug,
                    "choice": cs.canonical_choice(opts, slug, rec.ep.choice),
                    "for_users": rec.ep.for_users if rec.ep.for_users in self._slugs else "both",
                    "ts": to_sql(rec.ts),
                    "summary": rec.ep.summary,
                },
                [ev(rec)],
                rec.ep.confidence,
                category_item_id=cat_ids[slug],
                ref=rec.id,
            )
        for note in notes:
            jobs.insert_item(c, job.id, "note", note.payload(), [], note.confidence, ref=note.path)
        for eid, why in design.unmapped:
            rec = episodes[eid]
            jobs.insert_item(
                c,
                job.id,
                "unmapped",
                {
                    "episode_id": eid,
                    "why": why,
                    "category_phrase": rec.ep.category_phrase,
                    "choice": rec.ep.choice if rec.chosen else "",
                    "for_users": rec.ep.for_users if rec.ep.for_users in self._slugs else "both",
                    "ts": to_sql(rec.ts),
                    "summary": rec.ep.summary,
                },
                [ev(rec)],
                rec.ep.confidence,
                ref=eid,
                status="info",
            )
        jobs.update(
            c,
            job.id,
            status="review",
            error=None,
            skipped_out_of_scope=skipped + design.out_of_scope,
            cost_usd=jobs.spent(c, job.id),
        )

    # --- ⑦ review (thin wrappers so views never touch SQL) -----------------------------------

    async def review(self, job_id: int, kind: str) -> list[jobs.Item]:
        return await self._db.read(lambda c: jobs.items(c, job_id, kind))

    async def edit(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        return await self._db.write(fn)

    # --- ⑧ apply ------------------------------------------------------------------------------

    async def apply(self, job_id: int, *, delete_export: bool) -> dict[str, int]:
        """DB rows in one transaction, then notes through NoteStore, then the raw-text purge.
        Items are marked 'applied' as they land, so a retry after a failure never doubles up."""
        await self._db.write(lambda c: jobs.set_status(c, job_id, "applying", expect=("review",)))
        try:
            summary = await self._db.write(lambda c: self._apply_rows(c, job_id))
            summary["notes"] = await self._apply_notes(job_id)
        except Exception:
            await self._db.write(lambda c: jobs.update(c, job_id, status="review"))
            raise
        job = await self.job(job_id)
        await self._db.write(
            lambda c: c.execute(
                "UPDATE import_windows SET text = NULL, result_json = NULL WHERE job_id = ?",
                (job_id,),
            )
        )
        await self._db.run_raw(lambda c: c.execute("VACUUM"))
        if delete_export:
            self.export_path(job).unlink(missing_ok=True)
        await self._db.write(
            lambda c: jobs.update(
                c,
                job_id,
                status="done",
                summary_json=json.dumps({**summary, "export_deleted": delete_export}),
                cost_usd=jobs.spent(c, job_id),
            )
        )
        log.info("import applied", extra={"job_id": job_id, **summary})
        return summary

    def _apply_rows(self, c: sqlite3.Connection, job_id: int) -> dict[str, int]:
        users = {u.slug: u.id for u in self._users}
        admin = self._users[0].id
        out = {"categories": 0, "options": 0, "decisions": 0}
        cat_ids: dict[int, int] = {}  # category item id → categories.id
        for it in jobs.items(c, job_id, "category"):
            if it.status == "applied":
                cat_ids[it.id] = int(it.payload["applied_id"])
            if it.status != "approved":
                continue
            p = it.payload
            cat = cats.get_by_id(c, p["existing_id"]) if p.get("existing_id") else None
            if cat is None:
                res = cats.resolve(
                    c,
                    phrase=p["display_name"],
                    proposed_slug=p["slug"],
                    description=p["description"],
                    proposed_tau_days=p["recency_tau_days"],
                    create_new=True,
                    created_by="import",
                )
                if res.category is None:
                    raise ImportProblem(f"couldn't create category {p['slug']}: {res.error}")
                cat = res.category
                if res.status == "created":
                    c.execute(
                        "UPDATE categories SET display_name = ?, default_n = ?, "
                        "allow_generated = ? WHERE id = ?",
                        (p["display_name"], p["default_n"], int(p["allow_generated"]), cat.id),
                    )
                    out["categories"] += 1
            for alias in p["aliases"]:
                cats.add_alias(c, alias, cat.id)
            cat_ids[it.id] = cat.id
            _mark_applied(c, it, applied_id=cat.id)

        for it in jobs.items(c, job_id, "option"):
            cid = cat_ids.get(it.category_item_id or 0)
            if it.status != "approved" or cid is None:
                continue
            p = it.payload
            row = c.execute(
                "SELECT id FROM options WHERE category_id = ? AND lower(name) = lower(?)",
                (cid, p["name"]),
            ).fetchone()
            if row is None:
                cur = c.execute(
                    "INSERT INTO options(category_id, name, tags_json, base_weight, owner, "
                    "created_by) VALUES (?, ?, ?, ?, 'shared', 'import')",
                    (cid, p["name"], json.dumps(p["tags"], ensure_ascii=False), p["base_weight"]),
                )
                option_id = int(cur.lastrowid or 0)
                out["options"] += 1
            else:
                option_id = int(row[0])
            for slug, mult in p.get("prefs", {}).items():
                if slug in users:  # live feedback already there wins
                    c.execute(
                        "INSERT OR IGNORE INTO option_prefs(option_id, user_id, multiplier) "
                        "VALUES (?, ?, ?)",
                        (option_id, users[slug], mult),
                    )
            _mark_applied(c, it)

        for it in jobs.items(c, job_id, "decision"):
            cid = cat_ids.get(it.category_item_id or 0)
            if it.status != "approved" or cid is None:
                continue
            p = it.payload
            row = c.execute(
                "SELECT id FROM options WHERE category_id = ? AND lower(name) = lower(?)",
                (cid, p["choice"]),
            ).fetchone()
            c.execute(
                "INSERT INTO decisions(category_id, option_id, choice_text, for_users, asked_by, "
                "status, source, created_at) VALUES (?, ?, ?, ?, ?, 'accepted', 'import', ?)",
                (cid, row[0] if row else None, p["choice"], p["for_users"], admin, p["ts"]),
            )
            out["decisions"] += 1
            _mark_applied(c, it)
        return out

    async def _apply_notes(self, job_id: int) -> int:
        notes = [n for n in await self.review(job_id, "note") if n.status == "approved"]
        for it in notes:
            p = it.payload
            lines = "\n".join(f"- {ln['text']}" for ln in p["lines"])
            if not lines:
                continue
            path = p["path"]
            heading = "Constraints" if path.startswith("people/") else None
            exists = await self._store.exists(path)
            await self._store.write(
                path,
                mode="append" if exists else "create",
                content=lines,
                heading=heading if exists else None,
                title=p["title"],
                source=f"import:{job_id}",
            )
            await self._db.write(functools.partial(_mark_applied, it=it))
        return len(notes)


def _mark_submitted(c: sqlite3.Connection, batch_id: str, ids: Sequence[int]) -> None:
    c.executemany(
        "UPDATE import_windows SET status = 'submitted', batch_id = ?, attempts = attempts + 1 "
        "WHERE id = ?",
        [(batch_id, i) for i in ids],
    )


def _mark_applied(c: sqlite3.Connection, it: jobs.Item, **extra: Any) -> None:
    payload = json.dumps({**it.payload, **extra}, ensure_ascii=False)
    c.execute(
        "UPDATE import_items SET status = 'applied', payload_json = ? WHERE id = ?",
        (payload, it.id),
    )


def _catalog(
    c: sqlite3.Connection,
) -> tuple[list[cs.ExistingCategory], dict[str, str], dict[str, list[str]]]:
    existing = [
        cs.ExistingCategory(r["id"], r["slug"], r["display_name"], r["description"])
        for r in c.execute(
            "SELECT id, slug, display_name, description FROM categories WHERE merged_into IS NULL"
        )
    ]
    slug_of = {e.id: e.slug for e in existing}
    aliases: dict[str, str] = {}
    for r in c.execute("SELECT alias, category_id FROM category_aliases"):
        cat = cats.get_by_id(c, r[1])
        if cat is not None and cat.id in slug_of:
            aliases[r[0]] = slug_of[cat.id]
    options: dict[str, list[str]] = {}
    for r in c.execute("SELECT category_id, name FROM options WHERE active = 1"):
        if r[0] in slug_of:
            options.setdefault(slug_of[r[0]], []).append(r[1])
    return existing, aliases, options


def _describe(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"[:300]
