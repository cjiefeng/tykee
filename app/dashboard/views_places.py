"""Memory → Places (§10.5, §10.6, §11): places Tykee knows (rename, merge duplicates, delete,
attributes with their source), filters by area and attribute, your own areas ("Home = Bishan"),
pets, recent link resolutions with their status, and the ``places.*`` settings."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from app.dashboard import queries
from app.dashboard.core import DashboardDeps, back, render
from app.places import areas as area_db
from app.places import attributes as attrs
from app.places import links
from app.places import pets as pets_mod
from app.places.service import Place
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


def _in_area(p: Place, area: area_db.Area) -> bool:
    if p.area_id == area.id:
        return True
    if p.lat is None or p.lng is None:
        return False
    return links.distance_m(p.lat, p.lng, area.lat, area.lng) <= area.radius_m


def register(router: APIRouter, deps: DashboardDeps) -> None:
    @router.get(URL, response_class=HTMLResponse)
    async def places(request: Request, area: str = "", attr: str = "") -> Response:
        if deps.places is None:
            return back(request, "/memory", "Places aren't wired up in this process.", "error")
        shown = await deps.places.all()
        found = await deps.places.attributes([p.id for p in shown])
        area_hit, area_error = None, ""
        if area.strip():
            area_hit = await deps.db.read(lambda c: area_db.match(c, area))
            if area_hit is None:
                area_error = f"No area called {area.strip()!r}."
            else:
                shown = [p for p in shown if _in_area(p, area_hit)]
        if attr:
            key, _, value = attr.partition(":")
            shown = [
                p
                for p in shown
                if key in found.get(p.id, {}) and (not value or found[p.id][key].value == value)
            ]
        area_ids = {p.area_id for p in shown if p.area_id is not None}
        names = await deps.db.read(
            lambda c: {i: a.name for i in area_ids if (a := area_db.get(c, i)) is not None}
        )
        return render(
            request,
            deps,
            "places.html",
            s=await deps.settings.load(),
            places=shown,
            total=len(await deps.places.all()),
            attrs=found,
            area_names=names,
            values=attrs.VALUES,
            labels=attrs.LABELS,
            area=area,
            area_hit=area_hit,
            area_error=area_error,
            attr=attr,
            user_areas=await deps.db.read(area_db.user_areas),
            pets=await deps.memory.pets(),
            links=await deps.places.recent_links(),
            reactions=sorted(TELEGRAM_REACTIONS),
        )

    @router.post(URL + "/{place_id}/attribute")
    async def attribute(
        request: Request,
        place_id: int,
        key: str = Form(...),
        value: str = Form(...),
        evidence: str = Form(""),
    ) -> Response:
        assert deps.places is not None
        if await deps.places.get(place_id) is None:
            return back(request, URL, "Unknown place.", "error")
        if value == "remove":
            await deps.places.remove_attribute(place_id, key)
            return back(request, URL, "Attribute removed.")
        try:
            await deps.places.set_attribute(
                place_id,
                key,
                value,
                source="user",
                evidence=evidence.strip() or "set in the dashboard",
            )
        except attrs.AttrError as e:
            return back(request, URL, str(e), "error")
        return back(request, URL, "Saved as confirmed by you.")

    @router.post(URL + "/areas")
    async def add_area(
        request: Request, name: str = Form(...), like: str = Form(...), aliases: str = Form("")
    ) -> Response:
        extra = [a.strip() for a in aliases.split(",") if a.strip()]
        try:
            area, base = await deps.db.write(lambda c: area_db.save_user_area(c, name, like, extra))
        except area_db.AreaError as e:
            return back(request, URL + "#areas", str(e), "error")
        return back(request, URL + "#areas", f"{area.name} = around {base.name}.")

    @router.post(URL + "/areas/{area_id}/delete")
    async def delete_area(request: Request, area_id: int) -> Response:
        removed = await deps.db.write(lambda c: area_db.delete_user_area(c, area_id))
        return back(request, URL + "#areas", "Area removed." if removed else "Nothing to remove.")

    @router.post(URL + "/pets")
    async def save_pets(request: Request, pets: str = Form("")) -> Response:
        try:
            parsed = pets_mod.parse_lines(pets)
        except ValueError as e:
            return back(request, URL + "#pets", str(e), "error")
        await pets_mod.save(deps.store, parsed)
        return back(request, URL + "#pets", "Pets saved in shared/household.md.")

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
