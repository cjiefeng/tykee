"""Categories & Options page (§11, §8.1 sprawl control)."""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from app.dashboard import queries
from app.dashboard.core import DashboardDeps, back, render
from app.decisions import categories as cats
from app.decisions.categories import TAU_MAX, TAU_MIN


def _tags(raw: str) -> list[str]:
    return sorted({t.strip() for t in raw.replace("\n", ",").split(",") if t.strip()})


def register(router: APIRouter, deps: DashboardDeps) -> None:
    @router.get("/categories", response_class=HTMLResponse)
    async def categories(request: Request) -> Response:
        return render(request, deps, "categories.html", rows=await queries.categories(deps.db))

    @router.get("/categories/{category_id}", response_class=HTMLResponse)
    async def category(request: Request, category_id: int) -> Response:
        detail = await queries.category_detail(deps.db, category_id)
        if detail is None:
            return back(request, "/categories", "No such category.", "error")
        prefs: dict[int, list[tuple[str, float]]] = {}
        for p in detail["prefs"]:
            prefs.setdefault(p["option_id"], []).append((p["display_name"], p["multiplier"]))
        return render(
            request,
            deps,
            "category.html",
            **detail,
            pref_map=prefs,
            owners=[u.slug for u in deps.users] + ["shared"],
            tags_of=lambda o: ", ".join(json.loads(o["tags_json"])),
        )

    @router.post("/categories/{category_id}")
    async def save_category(
        request: Request,
        category_id: int,
        slug: str = Form(...),
        display_name: str = Form(...),
        description: str = Form(""),
        recency_tau_days: float = Form(...),
        default_n: int = Form(...),
        allow_generated: str = Form(""),
    ) -> Response:
        url = f"/categories/{category_id}"
        tau = min(max(recency_tau_days, TAU_MIN), TAU_MAX)
        n = min(max(default_n, 1), 5)

        def _save(c: sqlite3.Connection) -> None:
            cats.rename(c, category_id, slug=slug, display_name=display_name)
            c.execute(
                "UPDATE categories SET description = ?, recency_tau_days = ?, default_n = ?, "
                "allow_generated = ? WHERE id = ?",
                (description.strip(), tau, n, int(allow_generated == "on"), category_id),
            )

        try:
            await deps.db.write(_save)
        except ValueError as e:
            return back(request, url, str(e), "error")
        return back(request, url, "Saved.")

    @router.post("/categories/{category_id}/merge")
    async def merge(request: Request, category_id: int, into: int = Form(...)) -> Response:
        try:
            await deps.db.write(lambda c: cats.merge(c, category_id, into))
        except ValueError as e:
            return back(request, f"/categories/{category_id}", str(e), "error")
        return back(request, f"/categories/{into}", "Merged. The old slug now resolves here.")

    @router.post("/categories/{category_id}/aliases")
    async def add_alias(request: Request, category_id: int, alias: str = Form(...)) -> Response:
        await deps.db.write(lambda c: cats.add_alias(c, alias, category_id))
        return back(request, f"/categories/{category_id}", f"Alias {alias!r} added.")

    @router.post("/categories/{category_id}/aliases/delete")
    async def delete_alias(request: Request, category_id: int, alias: str = Form(...)) -> Response:
        await deps.db.write(
            lambda c: c.execute(
                "DELETE FROM category_aliases WHERE alias = ? AND category_id = ?",
                (alias, category_id),
            )
        )
        return back(request, f"/categories/{category_id}", f"Alias {alias!r} removed.")

    @router.post("/categories/{category_id}/options")
    async def add_option(
        request: Request,
        category_id: int,
        name: str = Form(...),
        tags: str = Form(""),
        owner: str = Form("shared"),
    ) -> Response:
        url = f"/categories/{category_id}"
        category = await deps.decisions.get_category(category_id)
        if category is None or not name.strip():
            return back(request, url, "Name is required.", "error")
        added = await deps.decisions.add_option(category, name, _tags(tags), owner)
        return back(request, url, "Option added." if added else "That option already exists.")

    @router.post("/options/{option_id}")
    async def save_option(
        request: Request,
        option_id: int,
        category_id: int = Form(...),
        name: str = Form(...),
        tags: str = Form(""),
        base_weight: float = Form(1.0),
        owner: str = Form("shared"),
        active: str = Form(""),
    ) -> Response:
        url = f"/categories/{category_id}"
        if owner not in [u.slug for u in deps.users] + ["shared"]:
            return back(request, url, "Unknown owner.", "error")
        try:
            await deps.db.write(
                lambda c: c.execute(
                    "UPDATE options SET name = ?, tags_json = ?, base_weight = ?, owner = ?, "
                    "active = ? WHERE id = ?",
                    (
                        name.strip(),
                        json.dumps(_tags(tags), ensure_ascii=False),
                        max(base_weight, 0.0),
                        owner,
                        int(active == "on"),
                        option_id,
                    ),
                )
            )
        except Exception as e:  # e.g. UNIQUE(category_id, name)
            return back(request, url, f"Not saved: {e}", "error")
        return back(request, url, "Option saved.")

    @router.post("/options/{option_id}/reset-prefs")
    async def reset_prefs(
        request: Request, option_id: int, category_id: int = Form(...)
    ) -> Response:
        await deps.db.write(
            lambda c: c.execute("DELETE FROM option_prefs WHERE option_id = ?", (option_id,))
        )
        return back(request, f"/categories/{category_id}", "Preferences reset.")
