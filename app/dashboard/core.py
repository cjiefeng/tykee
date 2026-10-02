"""Dashboard plumbing (§11): dependencies, LAN-only guard, login, sessions, CSRF, rendering.

Security model: LAN-only (requests from non-private addresses are refused), one admin password
(argon2 hash from env; no default, so no hash → no dashboard), signed session cookie
(SameSite=Strict, HttpOnly), and a per-session CSRF token required on every POST (form field
``csrf`` or header ``X-CSRF-Token`` for HTMX).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from app.ambient.service import AmbientService
from app.backup import BackupService
from app.brain.memory import MemoryService
from app.brain.store import NoteStore
from app.db.database import Database
from app.db.repos.users import UserRecord
from app.decisions.service import DecisionService
from app.harvest import Harvester
from app.health import HealthState
from app.importer.service import ImportService
from app.nudges import NudgeService
from app.settings import SettingsStore
from app.telegram.gateway import ChatGateway
from app.telegram.topics import TopicService
from app.timeutil import from_sql

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
SESSION_MAX_AGE = 7 * 24 * 3600
MAX_FAILURES = 5
LOCKOUT_S = 300
_LAN_NETS = [
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "100.64.0.0/10",  # Tailscale (CGNAT range), the only sanctioned remote access (§11)
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
]


@dataclass
class DashboardDeps:
    db: Database
    settings: SettingsStore
    store: NoteStore
    memory: MemoryService
    decisions: DecisionService
    topics: TopicService
    health: HealthState
    gateway: ChatGateway
    users: list[UserRecord]
    tz: ZoneInfo
    group_id: Callable[[], int | None]
    db_path: Path
    password_hash: str
    session_secret: str
    log_lines: Callable[[], list[str]]
    harvester: Harvester | None = None
    ambient: AmbientService | None = None
    importer: ImportService | None = None
    nudges: NudgeService | None = None
    backups: BackupService | None = None
    embed_model: str = ""
    _failures: dict[str, tuple[int, float]] = field(default_factory=dict)


def is_lan(host: str | None) -> bool:
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr in net for net in _LAN_NETS)


# --- login -----------------------------------------------------------------------------------

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


async def check_password(deps: DashboardDeps, client: str, password: str) -> bool:
    """argon2 verify in a thread (it's deliberately slow) with a per-client lockout."""
    count, until = deps._failures.get(client, (0, 0.0))
    if until > time.monotonic():
        return False

    def _verify() -> bool:
        try:
            return _hasher.verify(deps.password_hash, password)
        except (VerificationError, InvalidHashError):
            return False

    ok = await asyncio.to_thread(_verify)
    if ok:
        deps._failures.pop(client, None)
        return True
    count += 1
    deps._failures[client] = (
        (0, time.monotonic() + LOCKOUT_S) if count >= MAX_FAILURES else (count, 0.0)
    )
    log.warning("dashboard login failed", extra={"client": client, "failures": count})
    return False


def locked_out(deps: DashboardDeps, client: str) -> bool:
    return deps._failures.get(client, (0, 0.0))[1] > time.monotonic()


def start_session(request: Request) -> None:
    request.session.clear()
    request.session["auth"] = True
    request.session["csrf"] = secrets.token_urlsafe(32)


def logged_in(request: Request) -> bool:
    return bool(request.session.get("auth"))


async def check_csrf(request: Request) -> None:
    expected = request.session.get("csrf")
    sent = request.headers.get("x-csrf-token")
    if sent is None:
        form = await request.form()
        value = form.get("csrf")
        sent = value if isinstance(value, str) else None
    if not expected or not sent or not secrets.compare_digest(str(expected), sent):
        raise HTTPException(status_code=403, detail="CSRF check failed")


def flash(request: Request, message: str, kind: str = "ok") -> None:
    # Assign a new list: Starlette only re-sends the session cookie when the session itself is
    # modified, so mutating a list already stored in it would silently drop this message.
    request.session["flash"] = [*request.session.get("flash", []), [kind, message]]


def back(request: Request, url: str, message: str | None = None, kind: str = "ok") -> Response:
    """POST → redirect → GET, with an optional one-shot message."""
    if message:
        flash(request, message, kind)
    if request.headers.get("hx-request"):
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


# --- rendering -------------------------------------------------------------------------------

templates = Jinja2Templates(directory=str(HERE / "templates"))


def _ago(value: datetime | str | None) -> str:
    if value is None:
        return "never"
    dt = from_sql(value) if isinstance(value, str) else value
    secs = max(int((datetime.now(dt.tzinfo) - dt).total_seconds()), 0)
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if secs >= size:
            return f"{secs // size} {unit} ago"
    return "just now"


def _usd(value: float) -> str:
    return f"${value:,.2f}" if value >= 0.01 or value == 0 else f"${value:,.4f}"


templates.env.filters["ago"] = _ago
templates.env.filters["usd"] = _usd


def render(
    request: Request, deps: DashboardDeps, template: str, status_code: int = 200, **ctx: Any
) -> HTMLResponse:
    def _local(value: datetime | str | None, fmt: str = "%a %d %b %H:%M") -> str:
        if value is None:
            return ""
        dt = from_sql(value) if isinstance(value, str) else value
        return dt.astimezone(deps.tz).strftime(fmt)

    has_session = "session" in request.scope
    messages = request.session.pop("flash", []) if has_session else []
    token = str(request.session.get("csrf", "")) if has_session else ""
    return templates.TemplateResponse(
        request,
        template,
        {
            "csrf": token,
            "csrf_input": Markup(f'<input type="hidden" name="csrf" value="{escape(token)}">'),
            "flashes": messages,
            "local": _local,
            "nav": request.url.path,
            **ctx,
        },
        status_code=status_code,
    )
