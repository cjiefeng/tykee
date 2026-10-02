"""Behaviour, Ambient and raw Settings pages (§11). Every write goes through
``queries.save_settings``, which validates the whole settings set before writing."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from app.dashboard import queries, views_ops
from app.dashboard.core import DashboardDeps, back, render
from app.orchestrator.web import web_status
from app.settings import get_value
from app.timeutil import utcnow

PERSONA_HISTORY = "persona.history"
PERSONA_HISTORY_MAX = 20

MODEL_ROLES = [
    "default",
    "escalated",
    "deep",
    "judge",
    "harvest",
    "import_extract",
    "import_consolidate",
]
BEHAVIOUR_NUMBERS = {
    "history.max_turns": int,
    "llm.max_tokens": int,
    "decisions.session_hours": float,
    "budget.daily_usd": float,
    "budget.monthly_usd": float,
    "budget.warn_ratio": float,
    "memory.search_k": int,
    "memory.pinned_max_chars": int,
    "summary.batch": int,
    "escalation.long_message_chars": int,
    "escalation.max_tokens": int,
}
BEHAVIOUR_TOGGLES = ["memory.auto_approve", "escalation.enabled"]
ESCALATION_LISTS = ["escalation.think_phrases", "escalation.deep_phrases"]
WEB_NUMBERS = {
    "web.search_max_uses": int,
    "web.fetch_max_uses": int,
    "web.fetch_max_content_tokens": int,
    "web.daily_search_cap": int,
    "pricing.web_search": float,
}
RECOMMEND_NUMBERS = {
    "recommend.default_n": int,
    "recommend.default_radius_m": int,
    "recommend.explore_ratio": float,
    "recommend.web_attr_ttl_days": int,
    "recommend.max_web_searches": int,
}
WEB_LOCATION_FIELDS = ["city", "region", "country", "timezone"]
WEB_DOMAIN_LISTS = ["web.allowed_domains", "web.blocked_domains"]
AMBIENT_NUMBERS = {
    "ambient.debounce_s": float,
    "ambient.threshold": float,
    "ambient.cooldown_min": float,
    "ambient.max_per_day": int,
    "ambient.window_messages": int,
    "ambient.default_mute_min": float,
    "ambient.negative_window_min": float,
    "harvest.interval_min": float,
    "harvest.min_new_messages": int,
    "harvest.max_age_hours": float,
    "harvest.context_messages": int,
}
AMBIENT_TOGGLES = ["ambient.enabled", "harvest.enabled"]
AMBIENT_LISTS = ["ambient.negative_phrases", "ambient.mute_phrases"]


def _numbers(form: Any, spec: dict[str, type]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, kind in spec.items():
        raw = form.get(key)
        if isinstance(raw, str) and raw.strip():
            try:
                out[key] = kind(raw)
            except ValueError as e:
                raise queries.SettingsError(f"{key}: {raw!r} isn't a number") from e
    return out


def _toggles(form: Any, keys: list[str]) -> dict[str, bool]:
    return {k: form.get(k) == "on" for k in keys}


def register(router: APIRouter, deps: DashboardDeps) -> None:
    @router.get("/behaviour", response_class=HTMLResponse)
    async def behaviour(request: Request) -> Response:
        s = await deps.settings.load()
        history = await deps.db.read(lambda c: get_value(c, PERSONA_HISTORY)) or []
        return render(
            request,
            deps,
            "behaviour.html",
            s=s,
            roles=MODEL_ROLES,
            numbers=BEHAVIOUR_NUMBERS,
            history=list(reversed(history)),
            raw=await _raw_map(deps),
            web_numbers=WEB_NUMBERS,
            recommend_numbers=RECOMMEND_NUMBERS,
            web_status=await web_status(deps.db, s, deps.tz, deps.health),
            searches_today=await queries.web_searches_today(deps.db, utcnow(), deps.tz),
        )

    @router.post("/behaviour/persona")
    async def persona(request: Request, persona: str = Form(...)) -> Response:
        persona = persona.strip()
        if not persona:
            return back(request, "/behaviour", "The persona can't be empty.", "error")
        s = await deps.settings.load()
        if persona == s.persona_system_prompt:
            return back(request, "/behaviour", "No changes.")
        history = await deps.db.read(lambda c: get_value(c, PERSONA_HISTORY)) or []
        history.append(
            {"at": utcnow().isoformat(timespec="seconds"), "text": s.persona_system_prompt}
        )
        await queries.save_settings(
            deps.db,
            {"persona.system_prompt": persona, PERSONA_HISTORY: history[-PERSONA_HISTORY_MAX:]},
        )
        return back(request, "/behaviour", "Persona saved; the previous version is in history.")

    @router.post("/behaviour/settings")
    async def behaviour_settings(request: Request) -> Response:
        form = await request.form()
        try:
            changes: dict[str, Any] = {
                **_numbers(form, BEHAVIOUR_NUMBERS),
                **_toggles(form, BEHAVIOUR_TOGGLES),
            }
            for role in MODEL_ROLES:
                value = form.get(f"models.{role}")
                if isinstance(value, str) and value.strip():
                    changes[f"models.{role}"] = value.strip()
            for key in ESCALATION_LISTS:
                raw = form.get(key)
                if isinstance(raw, str):
                    changes[key] = [p.strip() for p in raw.splitlines() if p.strip()]
            await queries.save_settings(deps.db, changes)
        except queries.SettingsError as e:
            return back(request, "/behaviour", f"Not saved: {e}", "error")
        return back(request, "/behaviour", "Saved. Changes apply to the next message.")

    @router.post("/behaviour/web")
    async def web_settings(request: Request) -> Response:
        form = await request.form()
        try:
            changes: dict[str, Any] = {
                **_numbers(form, WEB_NUMBERS),
                **_toggles(form, ["web.enabled"]),
            }
            if isinstance(tier := form.get("web.tier"), str) and tier:
                changes["web.tier"] = tier
            for key in WEB_DOMAIN_LISTS:
                raw = form.get(key)
                if isinstance(raw, str):
                    changes[key] = [d.strip() for d in raw.split() if d.strip()]
            location = {
                f: v.strip()
                for f in WEB_LOCATION_FIELDS
                if isinstance(v := form.get(f"web.user_location.{f}"), str) and v.strip()
            }
            changes["web.user_location"] = {"type": "approximate", **location} if location else None
            await queries.save_settings(deps.db, changes)
        except queries.SettingsError as e:
            return back(request, "/behaviour", f"Not saved: {e}", "error")
        return back(request, "/behaviour", "Web settings saved. They apply to the next message.")

    @router.post("/behaviour/recommend")
    async def recommend_settings(request: Request) -> Response:
        form = await request.form()
        try:
            await queries.save_settings(deps.db, _numbers(form, RECOMMEND_NUMBERS))
        except queries.SettingsError as e:
            return back(request, "/behaviour#recommend", f"Not saved: {e}", "error")
        return back(request, "/behaviour#recommend", "Recommendation settings saved.")

    # --- ambient (§10.2) & harvester (§10.4) -------------------------------------------------

    @router.get("/ambient", response_class=HTMLResponse)
    async def ambient(request: Request) -> Response:
        s = await deps.settings.load()
        group = deps.group_id()
        return render(
            request,
            deps,
            "ambient.html",
            s=s,
            raw=await _raw_map(deps),
            numbers=AMBIENT_NUMBERS,
            log=await queries.ambient_log(deps.db),
            state=await queries.chat_state(deps.db, group),
            runs=await queries.harvest_runs(deps.db),
            cursors=await queries.harvest_cursors(deps.db),
            health=deps.health,
            **await views_ops.nudge_context(deps),
        )

    @router.post("/ambient/settings")
    async def ambient_settings(request: Request) -> Response:
        form = await request.form()
        try:
            changes: dict[str, Any] = {
                **_numbers(form, AMBIENT_NUMBERS),
                **_toggles(form, AMBIENT_TOGGLES),
            }
            for key in AMBIENT_LISTS:
                raw = form.get(key)
                if isinstance(raw, str):
                    changes[key] = [p.strip() for p in raw.splitlines() if p.strip()]
            prompt = form.get("ambient.judge_prompt")
            if isinstance(prompt, str) and prompt.strip():
                changes["ambient.judge_prompt"] = prompt.strip()
            await queries.save_settings(deps.db, changes)
        except queries.SettingsError as e:
            return back(request, "/ambient", f"Not saved: {e}", "error")
        return back(request, "/ambient", "Saved.")

    @router.post("/ambient/unmute")
    async def unmute(request: Request) -> Response:
        group = deps.group_id()
        if group is not None and deps.ambient is not None:
            await deps.ambient.unmute(group)
        return back(request, "/ambient", "Unmuted.")

    @router.post("/ambient/harvest")
    async def harvest_now(request: Request) -> Response:
        if deps.harvester is None:
            return back(request, "/ambient", "Harvester isn't running.", "error")
        results = await deps.harvester.tick(force=True)
        if not results:
            return back(request, "/ambient", "Checked: nothing new to harvest.")
        summary = ", ".join(f"topic {r.thread_id}: {r.status}" for r in results)
        return back(request, "/ambient", f"Harvested ({summary}).")

    # --- raw settings ------------------------------------------------------------------------

    @router.get("/settings", response_class=HTMLResponse)
    async def raw_settings(request: Request) -> Response:
        return render(request, deps, "settings.html", rows=await queries.raw_settings(deps.db))

    @router.post("/settings")
    async def save_raw(request: Request, key: str = Form(...), value: str = Form(...)) -> Response:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as e:
            return back(request, "/settings", f"{key}: not valid JSON ({e.msg}).", "error")
        try:
            await queries.save_settings(deps.db, {key.strip(): parsed})
        except queries.SettingsError as e:
            return back(request, "/settings", f"Not saved: {e}", "error")
        return back(request, "/settings", f"Saved {key}.")


async def _raw_map(deps: DashboardDeps) -> dict[str, Any]:
    return {k: json.loads(v) for k, v, _ in await queries.raw_settings(deps.db)}
