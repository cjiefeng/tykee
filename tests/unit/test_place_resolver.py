"""PlaceResolver (§10.5, §12): allowlisted redirects only, no body reads, cached results."""

from __future__ import annotations

import httpx

from app.places.resolver import MAX_HOPS, PlaceResolver
from tests.conftest import Env, maps_transport
from tests.unit.test_place_links import CID, HOME_Q, SHARED_Q

SHORT = "https://maps.app.goo.gl/AbC123xyz"


class _Exploding(httpx.AsyncByteStream):
    """A body that fails the test if anything tries to read it."""

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        raise AssertionError("response body was read")
        yield b""  # pragma: no cover


def _resolver(env: Env, handler: httpx.MockTransport) -> PlaceResolver:
    r = PlaceResolver(env.db, client=httpx.AsyncClient(transport=handler))
    env.closers.append(r.close)
    return r


async def test_short_link_follows_redirect_and_caches(env: Env) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": SHARED_Q}, stream=_Exploding())

    r = _resolver(env, httpx.MockTransport(handler))
    res = await r.resolve(SHORT)
    assert res.status == "resolved" and res.parsed is not None
    assert res.parsed.name == "Keisuke Tonkotsu King"
    assert res.parsed.google_id == f"cid:{CID}"
    assert res.final_url == SHARED_Q
    assert calls == [SHORT]  # the google.com/maps target is parsed, never fetched

    again = await r.resolve(SHORT)
    assert again.cached and again.status == "resolved" and calls == [SHORT]
    row = await env.db.read(
        lambda c: c.execute("SELECT status, attempts FROM place_links").fetchone()
    )
    assert tuple(row) == ("resolved", 1)


async def test_full_links_need_no_request(env: Env) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    r = _resolver(env, httpx.MockTransport(handler))
    res = await r.resolve(SHARED_Q)
    assert res.status == "resolved"


async def test_redirect_off_the_allowlist_is_blocked(env: Env) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/"})

    r = _resolver(env, httpx.MockTransport(handler))
    res = await r.resolve(SHORT)
    assert res.status == "blocked_host" and res.error == "169.254.169.254"
    assert calls == [SHORT]  # the internal address was never requested


async def test_redirect_loop_stops_after_max_hops(env: Env) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": f"https://goo.gl/maps/{len(calls)}"})

    r = _resolver(env, httpx.MockTransport(handler))
    res = await r.resolve(SHORT)
    assert res.status == "failed" and res.error == "too many redirects"
    assert len(calls) == MAX_HOPS


async def test_consent_interstitial_and_errors(env: Env) -> None:
    consent = f"https://consent.google.com/ml?continue={httpx.URL(SHARED_Q)}"
    r = _resolver(env, maps_transport({SHORT: consent}))
    assert (await r.resolve(SHORT)).status == "resolved"

    r2 = _resolver(env, maps_transport({}))  # 404
    res = await r2.resolve("https://maps.app.goo.gl/gone")
    assert res.status == "failed" and res.error == "http 404"

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    r3 = _resolver(env, httpx.MockTransport(boom))
    res = await r3.resolve("https://maps.app.goo.gl/slow")
    assert res.status == "failed" and res.error == "ConnectTimeout"


async def test_home_address_is_unnamed_and_final_url_not_kept(env: Env) -> None:
    r = _resolver(env, maps_transport({SHORT: HOME_Q}))
    res = await r.resolve(SHORT)
    assert res.status == "unnamed" and res.parsed is None
    row = await env.db.read(lambda c: c.execute("SELECT * FROM place_links").fetchone())
    assert row["status"] == "unnamed" and row["final_url"] is None
