"""Memory → Places (§10.5, §11): places Tykee knows from shared Maps links (rename, merge
duplicates, delete), recent link resolutions with their status, and the ``places.*`` settings."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from app.dashboard import queries
from app.dashboard.core import DashboardDeps, back, render
from app.settings import TELEGRAM_REACTIONS

URL = "/memory/places"


def parse_slots(raw: str) -> list[dict[str, str]]:
    """'17:00-23:00 dinner' per line → meal slot dicts (validated with the settings)."""
    slots: list[dict[str, str]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        span, _, category = line.strip().partition(" ")
        start, sep, end = span.partition("-")
        if not sep or not category.strip():
            raise queries.SettingsError(f"meal slot {line.strip()!r}: use 'HH:MM-HH:MM category'")
        slots.append({"start": start, "end": end, "category": category.strip()})
    return slots


def register(router: APIRouter, deps: DashboardDeps) -> None:
    @router.get(URL, response_class=HTMLResponse)
    async def places(request: Request) -> Response:
        if deps.places is None:
            return back(request, "/memory", "Places aren't wired up in this process.", "error")
        return render(
            request,
            deps,
            "places.html",
            s=await deps.settings.load(),
            places=await deps.places.all(),
            links=await deps.places.recent_links(),
            reactions=sorted(TELEGRAM_REACTIONS),
        )

    @router.post(URL + "/{place_id}/rename")
    async def rename(request: Request, place_id: int, name: str = Form(...)) -> Response:
        assert deps.places is not None
        try:
            place = await deps.places.rename(place_id, name)
        except ValueError as e:
            return back(request, URL, str(e), "error")
        return back(request, URL, f"Renamed to {place.name}.")

    @router.post(URL + "/{place_id}/merge")
    async def merge(request: Request, place_id: int, into: int = Form(...)) -> Response:
        assert deps.places is not None
        try:
            place = await deps.places.merge(place_id, into)
        except ValueError as e:
            return back(request, URL, str(e), "error")
        return back(request, URL, f"Merged into {place.name}.")

    @router.post(URL + "/{place_id}/delete")
    async def delete(request: Request, place_id: int) -> Response:
        assert deps.places is not None
        removed = await deps.places.delete(place_id)
        return back(request, URL, "Place deleted." if removed else "Nothing to delete.")

    @router.post(URL + "/settings")
    async def settings(request: Request) -> Response:
        form = await request.form()

        def text(key: str) -> str:
            value = form.get(key)
            return value if isinstance(value, str) else ""

        try:
            window = text("places.intent_window_s").strip()
            changes: dict[str, Any] = {
                "places.enabled": form.get("places.enabled") == "on",
                "places.reaction": text("places.reaction"),
                "places.intent_phrases": [
                    p.strip() for p in text("places.intent_phrases").splitlines() if p.strip()
                ],
                "places.meal_slots": parse_slots(text("places.meal_slots")),
            }
            if window:
                changes["places.intent_window_s"] = int(window)
            await queries.save_settings(deps.db, changes)
        except ValueError as e:  # SettingsError is a ValueError
            return back(request, URL, f"Not saved: {e}", "error")
        return back(request, URL, "Place settings saved. They apply to the next message.")
