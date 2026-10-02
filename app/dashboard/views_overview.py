"""Overview, Users/Telegram, System and Import pages (§11)."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from app.dashboard import queries
from app.dashboard.core import DashboardDeps, back, render
from app.llm.client import budget_status
from app.telegram.topics import send_thread
from app.timeutil import utcnow


def register(router: APIRouter, deps: DashboardDeps) -> None:
    @router.get("/", response_class=HTMLResponse)
    async def overview(request: Request) -> Response:
        s = await deps.settings.load()
        now = utcnow()
        spend = await queries.spend(deps.db, now, deps.tz)
        budget = await budget_status(deps.db, s, deps.tz)
        counts = await queries.counts(deps.db, now, deps.tz)
        errors, calls = deps.health.llm_error_rate()
        group = deps.group_id()
        answer = s.telegram_answer_topic_id
        answer_name = await deps.topics.name_of(group, answer) if group and answer else None
        return render(
            request,
            deps,
            "overview.html",
            s=s,
            spend=spend,
            budget=budget,
            counts=counts,
            health=deps.health,
            llm_errors=errors,
            llm_calls=calls,
            group=group,
            answer=answer,
            answer_name=answer_name,
            decisions=await queries.recent_decisions(deps.db),
            db_bytes=queries.db_size(deps.db_path),
            embed_model=deps.embed_model,
            warn=s.budget_warn_ratio,
        )

    # --- users & telegram (§10.4) ------------------------------------------------------------

    @router.get("/users", response_class=HTMLResponse)
    async def users(request: Request) -> Response:
        s = await deps.settings.load()
        group = deps.group_id()
        topics = await deps.topics.known(group) if group else []
        rows = await deps.db.read(lambda c: c.execute("SELECT * FROM users ORDER BY id").fetchall())
        return render(
            request,
            deps,
            "users.html",
            users=rows,
            admin_ids={u.id for u in deps.users if u.is_admin},
            group=group,
            topics=topics,
            s=s,
        )

    @router.post("/users/{user_id}")
    async def save_user(
        request: Request, user_id: int, display_name: str = Form(...), timezone: str = Form(...)
    ) -> Response:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return back(request, "/users", f"Unknown timezone {timezone!r}.", "error")
        await deps.db.write(
            lambda c: c.execute(
                "UPDATE users SET display_name = ?, timezone = ? WHERE id = ?",
                (display_name.strip(), timezone, user_id),
            )
        )
        return back(request, "/users", "Saved. Name and timezone changes apply after a restart.")

    @router.post("/telegram/answer-topic")
    async def answer_topic(request: Request, thread_id: str = Form("")) -> Response:
        try:
            new = int(thread_id) if thread_id.strip() else None
            await deps.topics.set_answer_topic(new)
        except ValueError as e:
            return back(request, "/users", f"Not saved: {e}", "error")
        deps.health.topic_ok()
        group = deps.group_id()
        if new is not None and group is not None:
            try:
                await deps.gateway.send_text(
                    group, "I'll hang out here now 👋", thread_id=send_thread(new)
                )
            except Exception as e:  # topic deleted/closed: keep the setting, report it
                deps.health.topic_error(f"thread {new}: {e}")
                return back(request, "/users", f"Saved, but posting there failed: {e}", "error")
        msg = (
            "Answer topic cleared: I answer in every topic." if new is None else "Answer topic set."
        )
        return back(request, "/users", msg)

    @router.post("/telegram/topics")
    async def topic_settings(request: Request) -> Response:
        form = await request.form()
        try:
            ignored = sorted({int(v) for v in form.getlist("ignored") if isinstance(v, str)})
        except ValueError:
            return back(request, "/users", "Topic ids must be numbers.", "error")
        mode = str(form.get("off_topic_mention", "ignore"))
        answer = (await deps.settings.load()).telegram_answer_topic_id
        if answer is not None and answer in ignored:
            return back(request, "/users", "The answer topic can't be ignored.", "error")
        try:
            await queries.save_settings(
                deps.db,
                {"telegram.ignored_topic_ids": ignored, "telegram.off_topic_mention": mode},
            )
        except queries.SettingsError as e:
            return back(request, "/users", str(e), "error")
        return back(request, "/users", "Topic settings saved.")

    @router.post("/telegram/topics/{thread_id}/label")
    async def label_topic(request: Request, thread_id: int, name: str = Form("")) -> Response:
        group = deps.group_id()
        if group is None:
            return back(request, "/users", "No group configured.", "error")
        await deps.topics.label(group, thread_id, name)
        return back(request, "/users", "Topic renamed.")

    # --- system ------------------------------------------------------------------------------

    @router.get("/system", response_class=HTMLResponse)
    async def system(request: Request) -> Response:
        counts = await queries.counts(deps.db, utcnow(), deps.tz)
        return render(
            request,
            deps,
            "system.html",
            counts=counts,
            db_bytes=queries.db_size(deps.db_path),
            db_path=deps.db_path,
            vault=deps.store.root,
            embed_model=deps.embed_model,
            health=deps.health,
            logs=deps.log_lines()[-200:],
        )

    @router.get("/system/logs", response_class=HTMLResponse)
    async def logs(request: Request) -> Response:
        return render(request, deps, "_logs.html", logs=deps.log_lines()[-200:])

    @router.post("/system/reindex")
    async def reindex(request: Request) -> Response:
        stats = await deps.store.rebuild()
        return back(
            request, "/system", f"Reindexed {stats['files']} notes ({stats['reindexed']} embedded)."
        )

    @router.get("/import", response_class=HTMLResponse)
    async def import_page(request: Request) -> Response:
        return render(request, deps, "import.html")
