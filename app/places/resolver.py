"""PlaceResolver (§10.5): pasted Maps link → final URL → parsed place, cached in ``place_links``.

Only short links (maps.app.goo.gl, goo.gl/maps) are fetched; full google.<tld>/maps links are
parsed as-is. Every hop must stay on an allowlisted host, at most 5 hops in 5 s, and response
bodies are never read (the stream is closed after the headers), so a pasted link can't make the
bot reach anything else (§12, SSRF). No LLM, no Maps API.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from urllib.parse import urljoin

import httpx

from app.db.database import Database
from app.places import links
from app.places.links import ParsedPlace
from app.timeutil import to_sql, utcnow

log = logging.getLogger(__name__)

Status = Literal["resolved", "unnamed", "failed", "blocked_host"]

MAX_HOPS = 5
TIMEOUT_S = 5.0
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)


@dataclass(frozen=True)
class Resolution:
    url: str
    status: Status
    parsed: ParsedPlace | None = None  # set when status == 'resolved'
    final_url: str | None = None
    error: str | None = None
    cached: bool = False


class PlaceResolver:
    def __init__(
        self,
        db: Database,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = TIMEOUT_S,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._client = client
        self._timeout_s = timeout_s
        self._clock = clock

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
                timeout=self._timeout_s,
                follow_redirects=False,
                trust_env=False,  # never route through a proxy from the environment
            )
        return self._client

    async def cached(self, url: str) -> Resolution | None:
        row = await self._db.read(
            lambda c: c.execute("SELECT * FROM place_links WHERE url = ?", (url,)).fetchone()
        )
        return _from_row(row) if row is not None else None

    async def resolve(self, url: str, *, refresh: bool = False) -> Resolution:
        """Cached unless ``refresh`` (the retry job): the same link is never fetched twice."""
        if not refresh and (hit := await self.cached(url)) is not None:
            return hit
        res = await self._resolve(url)
        if res.status == "failed":
            log.info("place link failed", extra={"error": res.error})
        if res.status != "unnamed" or links.is_short_link(url):
            # A full link that isn't a business carries the address itself: never kept (§10.5).
            await self._db.write(lambda c: _store(c, res, to_sql(self._clock())))
        return res

    async def _resolve(self, url: str) -> Resolution:
        try:
            async with asyncio.timeout(self._timeout_s):
                final = await self._follow(url)
        except TimeoutError:
            return Resolution(url, "failed", error="timeout")
        except httpx.HTTPError as e:
            return Resolution(url, "failed", error=type(e).__name__)
        if isinstance(final, Resolution):
            return final
        parsed = links.parse_maps_url(final)
        if parsed is None:
            return Resolution(url, "failed", error="unparseable")
        if not links.is_named_business(parsed.name):
            return Resolution(url, "unnamed")  # §10.5 privacy: the final URL isn't kept either
        return Resolution(url, "resolved", parsed=parsed, final_url=final)

    async def _follow(self, url: str) -> str | Resolution:
        """The first URL that can be parsed without fetching, or why there isn't one."""
        current = url
        hops = 0
        while True:
            if (target := links.consent_target(current)) is not None:
                current = target  # strictly shorter each time, so this ends
                continue
            if not links.host_allowed(current):
                return Resolution(url, "blocked_host", error=links.host_of(current))
            if not links.is_short_link(current):
                return current
            if hops == MAX_HOPS:
                return Resolution(url, "failed", error="too many redirects")
            hops += 1
            async with self._http().stream("GET", current) as r:
                location = r.headers.get("location") if r.is_redirect else None
                status = r.status_code
            if location is None:
                return Resolution(url, "failed", error=f"http {status}")
            current = urljoin(current, location)


def _store(c: sqlite3.Connection, res: Resolution, now: str) -> None:
    c.execute(
        "INSERT INTO place_links(url, final_url, status, error, attempts, resolved_at) "
        "VALUES (?, ?, ?, ?, 1, ?) ON CONFLICT(url) DO UPDATE SET final_url = excluded.final_url, "
        "status = excluded.status, error = excluded.error, attempts = attempts + 1, "
        "resolved_at = excluded.resolved_at",
        (res.url, res.final_url, res.status, res.error, now),
    )


def _from_row(r: sqlite3.Row) -> Resolution:
    status: Status = r["status"]
    parsed = None
    if status == "resolved" and r["final_url"]:
        parsed = links.parse_maps_url(r["final_url"])
        if parsed is None or not links.is_named_business(parsed.name):
            status, parsed = "unnamed", None  # stricter rules since it was cached
    return Resolution(r["url"], status, parsed, r["final_url"], r["error"], cached=True)
