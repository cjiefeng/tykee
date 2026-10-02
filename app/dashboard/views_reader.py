"""System → Account reader (§10.7): the reader's chat allowlist, consent, master switch,
Disconnect kill switch, Backfill and audit log.

Adding a chat and ticking consent need the admin password again (step-up auth, sharing the
login's lockout), so a stolen session cookie alone can't widen what Tykee reads.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from app.dashboard import core
from app.dashboard.core import DashboardDeps, back, render
from app.reader.service import ReaderProblem, ReaderService
from app.timeutil import utcnow

PAGE = "/system/reader"


def _client(request: Request) -> str:
    return request.client.host if request.client else "?"


def _int(value: Any, default: int | None = None) -> int | None:
    text = str(value or "").strip()
    if not text:
        return default
    try:
        return int(text)
    except ValueError as e:
        raise ReaderProblem(f"{text!r} isn't a number") from e


def register(router: APIRouter, deps: DashboardDeps) -> None:
    def _reader(request: Request) -> ReaderService | Response:
        if deps.reader is None:
            return back(request, PAGE, "The account reader isn't running.", "error")
        return deps.reader

    async def _step_up(request: Request, password: str) -> bool:
        if core.locked_out(deps, _client(request)):
            return False
        return await core.check_password(deps, _client(request), password)

    @router.get(PAGE, response_class=HTMLResponse)
    async def page(request: Request, ref: str = "", label: str = "") -> Response:
        r = deps.reader
        s = await deps.settings.load()
        return render(
            request,
            deps,
            "reader.html",
            reader=r,
            s=s,
            chats=await r.chats() if r else [],
            audit=await r.audit_log() if r else [],
            backfills=r.backfills if r else {},
            health=deps.health,
            ref=ref,
            label=label,
            today=utcnow().astimezone(deps.tz).date().isoformat(),
        )

    @router.get(PAGE + "/pick", response_class=HTMLResponse)
    async def pick(request: Request) -> Response:
        """ "Pick from my chats": 50 recent chat names, fetched now, never stored."""
        r = _reader(request)
        if isinstance(r, Response):
            return r
        try:
            dialogs = await r.dialogs()
        except ReaderProblem as e:
            return back(request, PAGE, f"Couldn't list chats: {e}", "error")
        group = deps.group_id()
        return render(request, deps, "reader_pick.html", dialogs=dialogs, group_id=group)

    @router.post(PAGE + "/enabled")
    async def enabled(request: Request) -> Response:
        r = _reader(request)
        if isinstance(r, Response):
            return r
        on = (await request.form()).get("enabled") == "on"
        try:
            await r.set_enabled(on)
        except ReaderProblem as e:
            return back(request, PAGE, str(e), "error")
        return back(request, PAGE, f"The account reader is {'on' if on else 'off'}.")

    @router.post(PAGE + "/add")
    async def add(request: Request) -> Response:
        r = _reader(request)
        if isinstance(r, Response):
            return r
        form = await request.form()
        if not await _step_up(request, str(form.get("password", ""))):
            return back(request, PAGE, "Wrong password: the chat wasn't added.", "error")
        try:
            topic = _int(form.get("topic"))
            info = await r.resolve(str(form.get("ref", "")), topic)
            ch = await r.add(
                info,
                label=str(form.get("label", "")),
                topic=topic,
                consent=form.get("consent") == "on",
            )
        except ReaderProblem as e:
            return back(request, PAGE, f"Not added: {e}", "error")
        extra = "" if ch.consent_at else " Tick consent before anything is read."
        return back(request, PAGE, f"Added '{ch.label}'.{extra}")

    @router.post(PAGE + "/{chat_id}/update")
    async def update(request: Request, chat_id: int) -> Response:
        r = _reader(request)
        if isinstance(r, Response):
            return r
        form = await request.form()
        try:
            await r.update(
                chat_id,
                label=str(form.get("label", "")),
                enabled=form.get("enabled") == "on",
                interval_min=_int(form.get("interval_min"), 30) or 30,
                retention_days=_int(form.get("retention_days"), 7) or 7,
            )
        except ReaderProblem as e:
            return back(request, PAGE, f"Not saved: {e}", "error")
        return back(request, PAGE, "Saved.")

    @router.post(PAGE + "/{chat_id}/consent")
    async def consent(request: Request, chat_id: int) -> Response:
        r = _reader(request)
        if isinstance(r, Response):
            return r
        form = await request.form()
        on = form.get("consent") == "on"
        if on and not await _step_up(request, str(form.get("password", ""))):
            return back(request, PAGE, "Wrong password: consent wasn't recorded.", "error")
        try:
            await r.set_consent(chat_id, on)
        except ReaderProblem as e:
            return back(request, PAGE, str(e), "error")
        return back(request, PAGE, "Consent recorded." if on else "Consent removed: not reading.")

    @router.post(PAGE + "/{chat_id}/remove")
    async def remove(request: Request, chat_id: int) -> Response:
        r = _reader(request)
        if isinstance(r, Response):
            return r
        try:
            deleted = await r.remove(chat_id)
        except ReaderProblem as e:
            return back(request, PAGE, str(e), "error")
        return back(request, PAGE, f"Removed; {deleted} raw messages deleted.")

    @router.post(PAGE + "/{chat_id}/backfill")
    async def backfill(request: Request, chat_id: int) -> Response:
        r = _reader(request)
        if isinstance(r, Response):
            return r
        try:
            ch = await r.chat(chat_id)
            if not ch.readable:
                raise ReaderProblem("tick consent and enable the chat first")
            r.start_backfill(chat_id)
        except ReaderProblem as e:
            return back(request, PAGE, f"No backfill: {e}", "error")
        return back(
            request,
            PAGE,
            "Backfill started. When it's done, continue in Import (same review steps).",
        )

    @router.post(PAGE + "/disconnect")
    async def disconnect(request: Request) -> Response:
        r = _reader(request)
        if isinstance(r, Response):
            return r
        outcome = await r.disconnect()
        return back(request, PAGE, f"Disconnected: {outcome}; the saved session was deleted.")
