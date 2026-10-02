"""Scheduled nudges (§10.3, shown on the Ambient page) and backups (§14.2, on System)."""

from __future__ import annotations

import re
import sqlite3
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from app.backup import NAME_RE, list_backups
from app.dashboard import queries
from app.dashboard.core import DashboardDeps, back
from app.settings import DAYS, get_value
from app.timeutil import utcnow


async def nudge_context(deps: DashboardDeps) -> dict[str, Any]:
    """Template context for the nudges section of the Ambient page."""
    runs = await deps.db.read(
        lambda c: c.execute("SELECT * FROM nudge_runs ORDER BY id DESC LIMIT 30").fetchall()
    )
    cats = [c for c in await queries.categories(deps.db) if c["merged_into"] is None]
    return {
        "nudge_runs": runs,
        "nudge_categories": [(c["slug"], c["display_name"]) for c in cats],
        "nudge_targets": ["group", *(u.slug for u in deps.users)],
        "days": DAYS,
    }


def backup_context(deps: DashboardDeps) -> dict[str, Any]:
    files = list_backups(deps.backups.backup_dir) if deps.backups else []
    latest = files[0] if files else None
    stale = latest is not None and utcnow() - latest.modified > timedelta(hours=36)
    return {"backups": files, "latest_backup": latest, "backup_stale": stale}


def _new_id(category: str, at: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9_-]+", "-", f"{category}-{at.replace(':', '')}".lower()).strip("-")
    nid, n = base[:28], 2
    while nid in taken:
        nid, n = f"{base[:28]}-{n}", n + 1
    return nid


def _raw_items(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    items = get_value(conn, "nudges.items")
    return list(items) if isinstance(items, list) else []


def register(router: APIRouter, deps: DashboardDeps) -> None:
    async def _save_items(request: Request, items: list[dict[str, Any]], msg: str) -> Response:
        try:
            await queries.save_settings(deps.db, {"nudges.items": items})
        except queries.SettingsError as e:
            return back(request, "/ambient#nudges", f"Not saved: {e}", "error")
        return back(request, "/ambient#nudges", msg)

    @router.post("/nudges/settings")
    async def nudge_settings(request: Request) -> Response:
        form = await request.form()
        changes: dict[str, Any] = {"nudges.enabled": form.get("nudges.enabled") == "on"}
        grace = form.get("nudges.grace_min")
        try:
            if isinstance(grace, str) and grace.strip():
                changes["nudges.grace_min"] = float(grace)
            await queries.save_settings(deps.db, changes)
        except (ValueError, queries.SettingsError) as e:
            return back(request, "/ambient#nudges", f"Not saved: {e}", "error")
        state = "on" if changes["nudges.enabled"] else "off"
        return back(request, "/ambient#nudges", f"Scheduled nudges are {state}.")

    @router.post("/nudges/add")
    async def add_nudge(request: Request) -> Response:
        form = await request.form()
        at = str(form.get("time", "")).strip()
        target = str(form.get("target", "group"))
        days = [d for d in form.getlist("days") if isinstance(d, str)]
        category = await deps.decisions.lookup(str(form.get("category", "")).strip())
        if category is None:
            return back(request, "/ambient#nudges", "Pick an existing category.", "error")
        items = await deps.db.read(_raw_items)
        nid = _new_id(category.slug, at, {str(i.get("id")) for i in items})
        item = {"id": nid, "time": at, "days": days, "category": category.slug, "target": target}
        return await _save_items(
            request, [*items, item], f"Nudge added: {category.display_name} at {at}."
        )

    @router.post("/nudges/{nudge_id}/toggle")
    async def toggle_nudge(request: Request, nudge_id: str) -> Response:
        items = await deps.db.read(_raw_items)
        for i in items:
            if i.get("id") == nudge_id:
                i["enabled"] = not i.get("enabled", True)
        return await _save_items(request, items, "Nudge updated.")

    @router.post("/nudges/{nudge_id}/delete")
    async def delete_nudge(request: Request, nudge_id: str) -> Response:
        items = [i for i in await deps.db.read(_raw_items) if i.get("id") != nudge_id]
        return await _save_items(request, items, "Nudge deleted.")

    @router.post("/nudges/{nudge_id}/send")
    async def send_nudge(request: Request, nudge_id: str) -> Response:
        if deps.nudges is None:
            return back(request, "/ambient#nudges", "Nudges aren't running.", "error")
        o = await deps.nudges.run_now(nudge_id)
        if o.status == "sent":
            return back(request, "/ambient#nudges", "Nudge sent.")
        return back(request, "/ambient#nudges", f"Not sent ({o.status}: {o.reason}).", "error")

    # --- backups -----------------------------------------------------------------------------

    @router.post("/system/backup")
    async def backup_now(request: Request) -> Response:
        if deps.backups is None:
            return back(request, "/system", "Backups aren't running.", "error")
        try:
            r = await deps.backups.run()
        except Exception as e:
            return back(request, "/system", f"Backup failed: {e}", "error")
        kind = "error" if r.vault.startswith("failed") else "ok"
        return back(request, "/system", f"Backed up to {r.db_file}; vault {r.vault}.", kind)

    @router.get("/system/backups/{name}")
    async def download_backup(name: str) -> Response:
        if deps.backups is None or not NAME_RE.match(name):
            raise HTTPException(404, "No such backup")
        path = deps.backups.backup_dir / name
        if not path.is_file():
            raise HTTPException(404, "No such backup")
        return FileResponse(path, filename=name, media_type="application/vnd.sqlite3")
