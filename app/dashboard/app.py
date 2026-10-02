"""FastAPI app for the admin dashboard (§11). Built by ``create_app(deps)`` and served by uvicorn
inside the bot's own event loop (one process, §3)."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from app.dashboard import core
from app.dashboard.core import DashboardDeps, render
from app.timeutil import utcnow


def deps_of(request: Request) -> DashboardDeps:
    deps: DashboardDeps = request.app.state.deps
    return deps


async def require_login(request: Request) -> None:
    if core.logged_in(request):
        return
    if request.headers.get("hx-request"):
        raise HTTPException(status_code=401, headers={"HX-Redirect": "/login"})
    raise HTTPException(status_code=303, headers={"Location": "/login"})


async def require_csrf(request: Request) -> None:
    if request.method == "POST":
        await core.check_csrf(request)


class LanOnly:
    """Refuse anything not from a private/loopback/Tailscale address (§11, §12)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            client = scope.get("client")
            if not core.is_lan(client[0] if client else None):
                response = Response("LAN only", status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(deps: DashboardDeps) -> FastAPI:
    from app.dashboard import (
        views_behaviour,
        views_categories,
        views_chat,
        views_import,
        views_memory,
        views_ops,
        views_overview,
        views_places,
    )

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.deps = deps
    app.mount("/static", StaticFiles(directory=str(core.HERE / "static")), name="static")

    public = APIRouter(dependencies=[Depends(require_csrf)])

    @public.get("/login")
    async def login_form(request: Request) -> Response:
        if core.logged_in(request):
            return RedirectResponse("/", status_code=303)
        request.session.setdefault("csrf", secrets.token_urlsafe(32))
        return render(
            request, deps, "login.html", bare=True, locked=core.locked_out(deps, _client(request))
        )

    @public.post("/login")
    async def login(request: Request, password: str = Form("")) -> Response:
        client = _client(request)
        if core.locked_out(deps, client):
            return render(request, deps, "login.html", status_code=429, bare=True, locked=True)
        if not await core.check_password(deps, client, password):
            return render(
                request,
                deps,
                "login.html",
                status_code=401,
                bare=True,
                error="Wrong password.",
                locked=core.locked_out(deps, client),
            )
        core.start_session(request)
        return RedirectResponse("/", status_code=303)

    @public.get("/healthz")
    async def healthz() -> JSONResponse:
        """Unauthenticated liveness for the container healthcheck (§14.1); LAN-only like the
        rest. Answering at all proves the event loop is alive; 503 if the scheduler stopped."""
        h = deps.health
        ok = h.scheduler_ok()
        return JSONResponse(
            {
                "ok": ok,
                "uptime_s": int((utcnow() - h.started_at).total_seconds()),
                "last_update_at": h.last_update_at.isoformat() if h.last_update_at else None,
                "last_tick_at": h.last_tick_at.isoformat() if h.last_tick_at else None,
            },
            status_code=200 if ok else 503,
        )

    private = APIRouter(dependencies=[Depends(require_login), Depends(require_csrf)])

    @private.post("/logout")
    async def logout(request: Request) -> Response:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    for module in (
        views_overview,
        views_behaviour,
        views_categories,
        views_memory,
        views_places,
        views_chat,
        views_import,
        views_ops,
    ):
        module.register(private, deps)

    app.include_router(public)
    app.include_router(private)

    @app.exception_handler(HTTPException)
    async def _redirects(request: Request, exc: HTTPException) -> Response:
        if exc.status_code in (303, 401) and exc.headers:
            if "Location" in exc.headers:
                return RedirectResponse(exc.headers["Location"], status_code=303)
            return Response(status_code=401, headers=exc.headers)
        return render(
            request, deps, "error.html", status_code=exc.status_code, detail=str(exc.detail)
        )

    # Middleware: added innermost first. Sessions wrap the routes; LAN check is outermost.
    app.add_middleware(
        SessionMiddleware,
        secret_key=deps.session_secret,
        session_cookie="tykee_session",
        max_age=core.SESSION_MAX_AGE,
        same_site="strict",
        https_only=False,  # plain HTTP on the LAN
    )
    app.add_middleware(LanOnly)
    return app


def _client(request: Request) -> str:
    return request.client.host if request.client else "?"


def form_dict(form: Any) -> dict[str, str]:
    return {k: v for k, v in form.items() if isinstance(v, str)}
