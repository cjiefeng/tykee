"""Google Maps links (§10.5), pure functions: which URLs count as Maps links, which hosts a
redirect may visit, the tolerant final-URL parser, the named-business privacy rule and the
inline message annotation. No I/O."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote_plus, urlsplit

from aiogram.types import MessageEntity

from app.decisions.text import normalise

LOCATION_SHARED = "⟦location shared⟧"
MAX_LINKS_PER_MESSAGE = 3

SHORT_HOSTS = frozenset({"maps.app.goo.gl", "goo.gl"})
_GOOGLE_HOST = re.compile(r"^(?:www\.|maps\.)?google\.(?:com|[a-z]{2}|com?\.[a-z]{2})$")
_CONSENT_HOST = re.compile(r"^consent\.google\.(?:com|[a-z]{2}|com?\.[a-z]{2})$")
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"'⟦⟧]+")
_TRAILING = ".,;:!?)]}>'\""

_PRECISE = re.compile(r"!3d(-?\d{1,3}\.\d+)!4d(-?\d{1,3}\.\d+)")
_AT = re.compile(r"/@(-?\d{1,3}\.\d+),(-?\d{1,3}\.\d+)")
_FTID = re.compile(r"^0x[0-9a-f]+:0x([0-9a-f]+)$", re.IGNORECASE)
_DATA_FTID = re.compile(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", re.IGNORECASE)
_COORDS = re.compile(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


# --- hosts & detection -----------------------------------------------------------------------


def _host(url: str) -> str | None:
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or u.username or u.password:
        return None
    if port not in (None, 80, 443):
        return None
    return (u.hostname or "").lower() or None


def host_of(url: str) -> str:
    return _host(url) or "?"


def host_allowed(url: str) -> bool:
    """Every redirect hop must stay on these hosts (§12, SSRF)."""
    host = _host(url)
    return host is not None and (host in SHORT_HOSTS or _GOOGLE_HOST.match(host) is not None)


def is_short_link(url: str) -> bool:
    """Links that need their redirects followed; everything else is parsed as-is."""
    return _host(url) in SHORT_HOSTS


def is_maps_link(url: str) -> bool:
    host = _host(url)
    if host is None:
        return False
    path = urlsplit(url).path
    if host == "maps.app.goo.gl":
        return len(path) > 1
    if host == "goo.gl":
        return path.startswith("/maps/")
    if _GOOGLE_HOST.match(host) is None:
        return False
    return host.startswith("maps.") or path == "/maps" or path.startswith("/maps/")


def consent_target(url: str) -> str | None:
    """Google's cookie-consent interstitial carries the real destination in ``continue``."""
    host = _host(url)
    if host is None or _CONSENT_HOST.match(host) is None:
        return None
    target = parse_qs(urlsplit(url).query).get("continue", [""])[0]
    return target or None


def _clean(url: str) -> str:
    return url.rstrip(_TRAILING)


def urls_in_text(text: str) -> list[str]:
    """Plain-text fallback (import exports have no entities)."""
    seen: list[str] = []
    for m in _URL_IN_TEXT.finditer(text):
        url = _clean(m.group(0))
        if is_maps_link(url) and url not in seen:
            seen.append(url)
    return seen


def urls_in_entities(body: str, entities: Sequence[MessageEntity]) -> list[tuple[str, bool]]:
    """Maps URLs from Telegram ``url`` / ``text_link`` entities, as (url, visible in text)."""
    out: list[tuple[str, bool]] = []
    for e in entities:
        if e.type == "url":
            url, visible = _clean(e.extract_from(body)), True
        elif e.type == "text_link" and e.url:
            url, visible = e.url, False
        else:
            continue
        if is_maps_link(url) and all(url != u for u, _ in out):
            out.append((url, visible))
    return out[:MAX_LINKS_PER_MESSAGE]


# --- parsing ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedPlace:
    name: str | None
    address: str | None = None
    lat: float | None = None
    lng: float | None = None
    google_id: str | None = None
    precise: bool = False  # name from /place/ or q= (True) vs a /search/ query (False)


def _valid(lat: float, lng: float) -> bool:
    return -90 <= lat <= 90 and -180 <= lng <= 180


def _cid_from_ftid(ftid: str) -> str | None:
    """The second half of a feature id is the place's CID, so ftid and cid links dedupe."""
    m = _FTID.match(ftid.strip())
    return f"cid:{int(m.group(1), 16)}" if m else None


def _split_name(raw: str) -> tuple[str | None, str | None]:
    """'Keisuke Tonkotsu King, 1 Tras Link, Singapore' → name + address."""
    text = " ".join(raw.split())
    if not text or text.startswith("place_id:"):
        return None, None
    if _COORDS.match(text):
        return None, None
    name, _, address = text.partition(", ")
    return name.strip() or None, address.strip() or None


def parse_maps_url(url: str) -> ParsedPlace | None:
    """Best-effort read of a (final) Google Maps URL. None if it isn't one; ``name`` None when
    it only carries coordinates."""
    if not is_maps_link(url) or is_short_link(url):
        return None
    u = urlsplit(url)
    qs = parse_qs(u.query)
    segments = [unquote_plus(s) for s in u.path.split("/") if s]

    lat = lng = None
    if (m := _PRECISE.search(url)) or (m := _AT.search(u.path)):
        lat, lng = float(m.group(1)), float(m.group(2))
    for key in ("q", "query", "ll"):
        if lat is None and (c := _COORDS.match(qs.get(key, [""])[0])):
            lat, lng = float(c.group(1)), float(c.group(2))
    if lat is not None and lng is not None and not _valid(lat, lng):
        lat = lng = None

    google_id = None
    if ftid := qs.get("ftid", [""])[0]:
        google_id = _cid_from_ftid(ftid)
    if google_id is None and (m := _DATA_FTID.search(url)):
        google_id = _cid_from_ftid(m.group(1))
    if google_id is None and (cid := qs.get("cid", [""])[0]).isdigit():
        google_id = f"cid:{int(cid)}"
    if google_id is None and (pid := qs.get("query_place_id", [""])[0]):
        google_id = f"gpid:{pid}"

    name = address = None
    precise = True
    if "place" in segments:
        i = segments.index("place")
        if i + 1 < len(segments) and not segments[i + 1].startswith(("@", "data=")):
            name, address = _split_name(segments[i + 1])
    elif "search" in segments:
        i = segments.index("search")
        if i + 1 < len(segments) and not segments[i + 1].startswith(("@", "data=")):
            name, address = _split_name(segments[i + 1])
            precise = False
    if name is None and "dir" not in segments:
        for key in ("q", "query"):
            if raw := qs.get(key, [""])[0]:
                name, address = _split_name(raw)
                if name is not None:
                    break
    return ParsedPlace(name, address, lat, lng, google_id, precise)


# --- privacy: named businesses only ----------------------------------------------------------

_STREET = (
    r"street|st|road|rd|avenue|ave|drive|dr|lane|ln|lorong|lor|jalan|jln|crescent|cres|close|"
    r"walk|way|link|boulevard|blvd|terrace|rise|circle|loop|highway|hwy"
)
_ADDRESS_PATTERNS = [
    re.compile(r"^\s*\d+[a-z]?\b.*\b(?:" + _STREET + r")\b", re.IGNORECASE),  # 123 Tampines St 11
    re.compile(r"\b(?:" + _STREET + r")\.?(?:\s+\d+[a-z]?)?\s*$", re.IGNORECASE),  # Tampines St 11
    re.compile(r"^\s*(?:blk|block)\b", re.IGNORECASE),
    re.compile(r"#\s?\d{1,3}\s?-\s?\d{1,5}"),  # unit number
    re.compile(r"\b\d{5,6}\b"),  # postal code
    re.compile(r"\d+\s*°"),  # 1°16'47.6"N
    re.compile(r"^[23456789CFGHJMPQRVWX]{4,8}\+[23456789CFGHJMPQRVWX]{2,3}\b", re.IGNORECASE),
]
_RESIDENTIAL = re.compile(
    r"\b(?:residences?|condominium|condo|apartments?|apt|hdb|flat)\b", re.IGNORECASE
)
_HOME_NAMES = frozenset({"home", "my home", "our home", "my place", "our place", "my house"})


def is_named_business(name: str | None) -> bool:
    """§10.5 privacy rule. Coordinates, street addresses, postal codes and residential buildings
    are never stored. Deliberately strict: a business that looks like an address is lost, but a
    home is never kept."""
    if not name or not name.strip():
        return False
    text = " ".join(name.split())
    if _COORDS.match(text) or normalise(text) in _HOME_NAMES:
        return False
    if _RESIDENTIAL.search(text):
        return False
    return not any(p.search(text) for p in _ADDRESS_PATTERNS)


# --- annotation ------------------------------------------------------------------------------

_ANNOTATION = re.compile(r"⟦place: ([^⟧]*)⟧")


def annotation(
    name: str,
    *,
    address: str | None = None,
    lat: float | None = None,
    lng: float | None = None,
    place_id: int | None = None,
) -> str:
    """``⟦place: Keisuke Tonkotsu King · 1 Tras Link · 1.2799,103.8443 · place_id=42⟧``."""
    parts = [_inline(name)]
    if address:
        parts.append(_inline(address)[:80])
    if lat is not None and lng is not None:
        parts.append(f"{lat:.4f},{lng:.4f}")
    if place_id is not None:
        parts.append(f"place_id={place_id}")
    return "⟦place: " + " · ".join(parts) + "⟧"


def _inline(text: str) -> str:
    return " ".join(text.replace("⟦", "(").replace("⟧", ")").replace("·", "-").split())


@dataclass(frozen=True)
class Annotated:
    name: str
    place_id: int | None


def annotations_in(text: str) -> list[Annotated]:
    out: list[Annotated] = []
    for m in _ANNOTATION.finditer(text):
        parts = [p.strip() for p in m.group(1).split(" · ")]
        pid = None
        if parts[-1].startswith("place_id=") and parts[-1][9:].isdigit():
            pid = int(parts[-1][9:])
        out.append(Annotated(parts[0], pid))
    return out


def match_place(choice: str, marked: Sequence[Annotated]) -> int | None:
    """The place_id a model-extracted choice refers to: same normalised name, or a fuzzy
    match (rapidfuzz ratio ≥ 90, as in import consolidation)."""
    from rapidfuzz import fuzz

    want = normalise(choice)
    if not want:
        return None
    for a in reversed(marked):
        have = normalise(a.name)
        if a.place_id is not None and (have == want or fuzz.ratio(have, want) >= 90):
            return a.place_id
    return None


def strip_markup(text: str) -> str:
    """Text without URLs and ⟦…⟧ markers, for phrase matching."""
    return re.sub(r"⟦[^⟧]*⟧", " ", _URL_IN_TEXT.sub(" ", text))


# --- geometry --------------------------------------------------------------------------------


def distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Haversine distance in metres."""
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
