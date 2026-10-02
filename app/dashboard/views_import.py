"""Import page (§15.2): the Telegram export wizard. Upload → validate → chats, range and senders
→ preview & cost with consent → run (progress, cancel) → review → apply."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from app.dashboard.core import DashboardDeps, back, render
from app.importer import jobs
from app.importer.jobs import ImportProblem
from app.importer.service import DuplicateUpload, ImportService
from app.importer.telegram import ExportError

TABS = ("categories", "options", "notes", "decisions", "unmapped")
_KIND = {
    "categories": "category",
    "options": "option",
    "notes": "note",
    "decisions": "decision",
    "unmapped": "unmapped",
}


def _url(job_id: int, tab: str | None = None) -> str:
    return f"/import/{job_id}" + (f"?tab={tab}" if tab else "")


def _senders(job: jobs.Job) -> list[tuple[str, str, int]]:
    """(ref, name, messages) across every chat in the export, busiest first."""
    agg: dict[str, tuple[str, int]] = {}
    for chat in job.meta.get("chats", []):
        for ref, (name, n) in chat["senders"].items():
            agg[ref] = (name, agg.get(ref, (name, 0))[1] + n)
    return sorted(((r, n, c) for r, (n, c) in agg.items()), key=lambda t: -t[2])


def register(router: APIRouter, deps: DashboardDeps) -> None:
    def svc() -> ImportService:
        if deps.importer is None:
            raise ImportProblem("the import isn't available (no data directory)")
        return deps.importer

    async def attempt(
        request: Request, url: str, fn: Callable[[], Awaitable[Any]], ok: str
    ) -> Response:
        try:
            await fn()
        except (ImportProblem, ExportError, ValueError) as e:
            return back(request, url, str(e), "error")
        return back(request, url, ok)

    @router.get("/import", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        rows = await deps.db.read(jobs.all_jobs)
        s = await deps.settings.load()
        return render(
            request,
            deps,
            "import.html",
            jobs=rows,
            max_mb=s.import_max_upload_mb,
            available=deps.importer is not None,
        )

    @router.post("/import/upload")
    async def upload(request: Request, export: Annotated[UploadFile, File()]) -> Response:
        s = await deps.settings.load()
        size = int(request.headers.get("content-length") or 0)
        if size > (s.import_max_upload_mb + 1) * 1024 * 1024:
            return back(
                request, "/import", f"Too big: the limit is {s.import_max_upload_mb} MB.", "error"
            )
        try:
            job_id = await svc().receive(export.file, export.filename or "result.json")
        except DuplicateUpload as e:
            return back(request, _url(e.job_id), str(e), "error")
        except (ImportProblem, ExportError) as e:
            return back(request, "/import", f"Can't import that file: {e}", "error")
        finally:
            await export.close()
        return back(request, _url(job_id), "Export checked. Choose what to import below.")

    @router.get("/import/{job_id}", response_class=HTMLResponse)
    async def job_page(request: Request, job_id: int, tab: str = "categories") -> Response:
        try:
            job = await svc().job(job_id)
        except ImportProblem as e:
            return back(request, "/import", str(e), "error")
        ctx: dict[str, Any] = {"job": job, "users": [u.slug for u in deps.users]}
        if job.status in jobs.EDITABLE:
            ctx["senders"] = _senders(job)
        if job.status == "configured":
            ctx["preview"] = await svc().preview(job_id)
        if job.status in (*jobs.ACTIVE, "review", "done", "failed", "cancelled"):
            ctx["progress"] = await svc().progress(job_id)
        if job.status in ("review", "done"):
            tab = tab if tab in TABS else "categories"
            items = await svc().review(job_id, _KIND[tab])
            all_cats = await svc().review(job_id, "category")
            ctx.update(
                tab=tab,
                tabs=TABS,
                items=items,
                categories=[c for c in all_cats if c.status not in ("merged",)],
                cat_status={c.id: c.status for c in all_cats},
                counts=await deps.db.read(lambda c: jobs.counts(c, job_id)),
            )
        return render(request, deps, "import_job.html", **ctx)

    @router.get("/import/{job_id}/progress", response_class=HTMLResponse)
    async def progress(request: Request, job_id: int) -> Response:
        job = await svc().job(job_id)
        if job.status not in jobs.ACTIVE:
            # Done (or failed): reload the whole page to show the next step.
            return Response(status_code=204, headers={"HX-Redirect": _url(job_id)})
        return render(
            request,
            deps,
            "_import_progress.html",
            job=job,
            progress=await svc().progress(job_id),
        )

    @router.post("/import/{job_id}/configure")
    async def configure(request: Request, job_id: int) -> Response:
        form = await request.form()
        job = await svc().job(job_id)
        chats = [str(v) for v in form.getlist("chats")]
        senders = {ref: str(form.get(f"sender:{ref}", "other")) for ref in job.sender_map}
        try:
            since = date.fromisoformat(str(form.get("since", "")))
            until = date.fromisoformat(str(form.get("until", "")))
        except ValueError:
            return back(request, _url(job_id), "Dates must be YYYY-MM-DD.", "error")
        return await attempt(
            request,
            _url(job_id),
            lambda: svc().configure(
                job_id, chats=chats, since=since, until=until, sender_map=senders
            ),
            "Windowed. Check the preview and estimate below.",
        )

    @router.post("/import/{job_id}/start")
    async def start(request: Request, job_id: int, consent: str = Form("")) -> Response:
        return await attempt(
            request,
            _url(job_id),
            lambda: svc().start(job_id, consent=consent == "on"),
            "Started. Extraction runs as a batch; this page updates by itself.",
        )

    @router.post("/import/{job_id}/cancel")
    async def cancel(request: Request, job_id: int) -> Response:
        return await attempt(request, _url(job_id), lambda: svc().cancel(job_id), "Cancelled.")

    @router.post("/import/{job_id}/reconfigure")
    async def reconfigure(request: Request, job_id: int) -> Response:
        async def _go() -> None:
            await deps.db.write(
                lambda c: jobs.set_status(c, job_id, "uploaded", expect=("configured",))
            )

        return await attempt(request, _url(job_id), _go, "Back to the settings.")

    # --- review ------------------------------------------------------------------------------

    @router.post("/import/{job_id}/items/{item_id}")
    async def decide(
        request: Request, job_id: int, item_id: int, action: str = Form(...), tab: str = Form("")
    ) -> Response:
        return await attempt(
            request,
            _url(job_id, tab),
            lambda: svc().edit(lambda c: jobs.decide(c, job_id, item_id, action == "approve")),
            "Approved." if action == "approve" else "Rejected.",
        )

    @router.post("/import/{job_id}/bulk")
    async def bulk(
        request: Request, job_id: int, tab: str = Form(...), min_conf: float = Form(0.8)
    ) -> Response:
        url = _url(job_id, tab)
        try:
            n = await svc().edit(
                lambda c: jobs.bulk_approve(c, job_id, _KIND.get(tab, ""), min_conf)
            )
        except ImportProblem as e:
            return back(request, url, str(e), "error")
        return back(request, url, f"Approved {n} with confidence ≥ {min_conf:g}.")

    @router.post("/import/{job_id}/categories/{item_id}")
    async def edit_category(
        request: Request,
        job_id: int,
        item_id: int,
        slug: str = Form(...),
        display_name: str = Form(...),
        description: str = Form(""),
        recency_tau_days: float = Form(...),
        default_n: int = Form(1),
        aliases: str = Form(""),
    ) -> Response:
        return await attempt(
            request,
            _url(job_id, "categories"),
            lambda: svc().edit(
                lambda c: jobs.edit_category(
                    c,
                    job_id,
                    item_id,
                    slug=slug,
                    display_name=display_name,
                    description=description,
                    tau=recency_tau_days,
                    default_n=default_n,
                    aliases=aliases.replace("\n", ",").split(","),
                )
            ),
            "Category saved.",
        )

    @router.post("/import/{job_id}/categories/{item_id}/merge")
    async def merge(request: Request, job_id: int, item_id: int, into: int = Form(...)) -> Response:
        return await attempt(
            request,
            _url(job_id, "categories"),
            lambda: svc().edit(lambda c: jobs.merge_categories(c, job_id, item_id, into)),
            "Merged. Its options, decisions and aliases moved over.",
        )

    @router.post("/import/{job_id}/options/{item_id}")
    async def edit_option(
        request: Request,
        job_id: int,
        item_id: int,
        name: str = Form(...),
        tags: str = Form(""),
        base_weight: float = Form(1.0),
    ) -> Response:
        return await attempt(
            request,
            _url(job_id, "options"),
            lambda: svc().edit(
                lambda c: jobs.edit_option(
                    c, job_id, item_id, name=name, tags=tags.split(","), base_weight=base_weight
                )
            ),
            "Option saved.",
        )

    @router.post("/import/{job_id}/notes/{item_id}")
    async def edit_note(
        request: Request, job_id: int, item_id: int, text: str = Form("")
    ) -> Response:
        return await attempt(
            request,
            _url(job_id, "notes"),
            lambda: svc().edit(lambda c: jobs.edit_note(c, job_id, item_id, text)),
            "Note saved.",
        )

    @router.post("/import/{job_id}/unmapped/{item_id}")
    async def assign(
        request: Request, job_id: int, item_id: int, category: int = Form(...)
    ) -> Response:
        return await attempt(
            request,
            _url(job_id, "unmapped"),
            lambda: svc().edit(lambda c: jobs.assign_unmapped(c, job_id, item_id, category)),
            "Assigned.",
        )

    @router.post("/import/{job_id}/apply")
    async def apply(request: Request, job_id: int, delete_export: str = Form("")) -> Response:
        try:
            summary = await svc().apply(job_id, delete_export=delete_export == "on")
        except (ImportProblem, ValueError) as e:
            return back(request, _url(job_id), str(e), "error")
        done = ", ".join(f"{v} {k}" for k, v in summary.items())
        return back(request, _url(job_id), f"Applied: {done}. Raw chat text purged.")
