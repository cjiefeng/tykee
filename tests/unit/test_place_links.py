"""§10.5 pure parts: link detection, host allowlist, the tolerant URL parser, the named-business
privacy rule and annotations. URL fixtures follow the shapes of real shared links (ids made up)."""

from __future__ import annotations

from datetime import time

import pytest

from app.places import links
from app.places.intent import has_intent, slot_category
from app.settings import MealSlot
from tests.conftest import url_entities

FTID = "0x31da196d2b1a1e3b:0x6f3e4b0c1f1b2a7d"
CID = int("6f3e4b0c1f1b2a7d", 16)

# What a maps.app.goo.gl share link redirects to (name + address in q, feature id in ftid).
SHARED_Q = (
    "https://www.google.com/maps?q=Keisuke+Tonkotsu+King,+1+Tras+Link,+%2301-11+Orchid+Hotel,"
    f"+Singapore+078867&ftid={FTID}&entry=gps&g_ep=CAESBzI0LjE1LjEYACC"
)
# A place page copied from the browser's address bar.
PLACE_PAGE = (
    "https://www.google.com/maps/place/Keisuke+Tonkotsu+King/@1.2799,103.8443,17z/data="
    f"!3m1!4b1!4m6!3m5!1s{FTID}!8m2!3d1.2799123!4d103.8443456!16s%2Fg%2F11c1q9t9qv?entry=ttu"
)
HOME_Q = "https://www.google.com/maps?q=Blk+123+Tampines+Street+11,+Singapore+521123&ftid=0x1:0x2"
PIN = "https://www.google.com/maps?q=1.3521,103.8198"

# A share.google link (the share sheet in the Google app / Maps): two hops, ending on a search
# results page with the place name in q and its knowledge-graph id in kgmid.
SHARE = "https://share.google/aBcD1234efGH5678"
SHARE_HOP = "https://www.google.com/share.google?q=aBcD1234efGH5678"
SHARE_SEARCH = (
    "https://www.google.com/search?kgmid=/g/11c1q9t9qv&hl=en-SG&q=Keisuke+Tonkotsu+King"
    "&shndl=30&source=sh/x/loc/uni/m1/1&kgs=0123456789abcdef&shem=lcuae,uaasie"
)
SHARE_NOT_PLACE = (
    "https://www.google.com/search?kgmid=/m/0abc12&q=Some+Film&source=sh/x/kp/osrp/m1/1"
)


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://maps.app.goo.gl/AbC123xyz", True),
        ("https://goo.gl/maps/AbC123", True),
        ("https://goo.gl/xyz", False),  # generic goo.gl, not Maps
        (SHARED_Q, True),
        (PLACE_PAGE, True),
        ("https://maps.google.com/?cid=123", True),
        ("https://www.google.com.sg/maps/place/Foo", True),
        ("https://www.google.com/search?q=ramen", False),
        (SHARE, True),
        ("https://share.google/", False),
        (SHARE_HOP, False),  # only fetched as a redirect hop, never picked up from text
        ("https://evil.example/maps/place/Foo", False),
        ("https://maps.google.com.evil.example/?q=Foo", False),
        ("https://user@maps.google.com/?q=Foo", False),
        ("https://maps.google.com:8443/?q=Foo", False),
        ("ftp://maps.google.com/?q=Foo", False),
    ],
)
def test_is_maps_link(url: str, ok: bool) -> None:
    assert links.is_maps_link(url) is ok


def test_host_allowlist_for_redirect_hops() -> None:
    assert links.host_allowed("https://maps.app.goo.gl/x")
    assert links.host_allowed("https://www.google.co.uk/maps?q=x")
    assert not links.host_allowed("http://169.254.169.254/latest/meta-data")
    assert not links.host_allowed("http://localhost:8081/")
    assert not links.host_allowed("https://consent.google.com/ml?continue=x")
    target = "https://www.google.com/maps?q=Foo"
    assert links.consent_target(f"https://consent.google.com/ml?continue={target}") == target


def test_parse_shared_short_link_target() -> None:
    p = links.parse_maps_url(SHARED_Q)
    assert p is not None
    assert p.name == "Keisuke Tonkotsu King"
    assert p.address == "1 Tras Link, #01-11 Orchid Hotel, Singapore 078867"
    assert p.google_id == f"cid:{CID}"
    assert p.lat is None


def test_share_google_hops_and_search_parser() -> None:
    assert links.is_short_link(SHARE) and links.is_short_link(SHARE_HOP)
    assert links.host_allowed(SHARE) and links.host_allowed(SHARE_HOP)
    assert not links.is_short_link(SHARE_SEARCH)
    p = links.parse_share_search(SHARE_SEARCH)
    assert p is not None
    assert p.name == "Keisuke Tonkotsu King" and p.address is None
    assert p.google_id == "kgmid:/g/11c1q9t9qv" and p.lat is None
    assert links.parse_share_search(SHARE_NOT_PLACE) is None  # not marked as a location
    assert links.parse_share_search(SHARE_SEARCH.replace("kgmid=", "x=")) is None
    assert links.parse_share_search("https://www.google.com/search?q=ramen") is None
    assert links.parse_share_search(SHARED_Q) is None  # a Maps URL isn't a share search


def test_parse_place_page_prefers_precise_coordinates() -> None:
    p = links.parse_maps_url(PLACE_PAGE)
    assert p is not None
    assert p.name == "Keisuke Tonkotsu King"
    assert (p.lat, p.lng) == (1.2799123, 103.8443456)  # !3d/!4d, not the @ viewport
    assert p.google_id == f"cid:{CID}"  # ftid inside data= dedupes with the q/ftid form


def test_parse_cid_search_and_pins() -> None:
    cid = links.parse_maps_url(f"https://maps.google.com/?cid={CID}")
    assert cid is not None and cid.google_id == f"cid:{CID}" and cid.name is None
    search = links.parse_maps_url(
        "https://www.google.com/maps/search/Ramen+Keisuke/@1.27,103.84,15z"
    )
    assert search is not None and search.name == "Ramen Keisuke" and not search.precise
    assert (search.lat, search.lng) == (1.27, 103.84)
    pin = links.parse_maps_url(PIN)
    assert pin is not None and pin.name is None and (pin.lat, pin.lng) == (1.3521, 103.8198)
    assert links.parse_maps_url("https://www.google.com/maps/@1.3,103.8,14z").name is None  # type: ignore[union-attr]
    assert links.parse_maps_url("https://maps.app.goo.gl/x") is None  # short links aren't parsed


@pytest.mark.parametrize(
    ("name", "business"),
    [
        ("Keisuke Tonkotsu King", True),
        ("328 Katong Laksa", True),  # leading number, no street word
        ("7-Eleven", True),
        ("Amoy Street Food Centre", True),
        ("Subway", True),
        ("Blk 123 Tampines Street 11", False),
        ("123 Tampines St 11", False),
        ("Tampines Street 11", False),
        ("Orchard Road", False),
        ("Singapore 521123", False),
        ("The Sail #12-34", False),
        ("Marina One Residences", False),
        ("Parc Esta Condo", False),
        ("Home", False),
        ("my place", False),
        ("1.3521,103.8198", False),
        ("1°21'07.6\"N 103°49'11.3\"E", False),
        ("6PH57VP3+QX", False),
        ("", False),
        (None, False),
    ],
)
def test_named_business_rule(name: str | None, business: bool) -> None:
    assert links.is_named_business(name) is business


def test_entities_and_text_urls() -> None:
    body = "eating here 👉 https://maps.app.goo.gl/AbC123 and https://example.com/x"
    ents = url_entities(body, "https://maps.app.goo.gl/AbC123", "https://example.com/x")
    assert links.urls_in_entities(body, ents) == [("https://maps.app.goo.gl/AbC123", True)]
    assert links.urls_in_text(f"see ({SHARED_Q}).") == [SHARED_Q]


def test_annotation_round_trip() -> None:
    marker = links.annotation(
        "Keisuke · King", address="1 Tras Link", lat=1.27991, lng=103.84431, place_id=42
    )
    assert marker == "⟦place: Keisuke - King · 1 Tras Link · 1.2799,103.8443 · place_id=42⟧"
    found = links.annotations_in(f"eating here {marker} ok")
    assert [(a.name, a.place_id) for a in found] == [("Keisuke - King", 42)]
    assert links.match_place("keisuke-king", found) == 42
    assert links.match_place("Ippudo", found) is None
    assert links.strip_markup(f"eating here https://maps.app.goo.gl/x {marker}").split() == [
        "eating",
        "here",
    ]


def test_intent_and_meal_slots() -> None:
    phrases = ["eating here", "let's go this one"]
    assert has_intent("Eating here! https://maps.app.goo.gl/x", phrases)
    assert has_intent("ok let's go this one", phrases)
    assert not has_intent("this place looks nice https://maps.app.goo.gl/x", phrases)
    slots = [
        MealSlot(start="10:30", end="15:00", category="lunch"),
        MealSlot(start="17:00", end="23:00", category="dinner"),
        MealSlot(start="23:00", end="03:00", category="supper"),
    ]
    assert slot_category(time(12, 0), slots) == "lunch"
    assert slot_category(time(19, 0), slots) == "dinner"
    assert slot_category(time(1, 30), slots) == "supper"  # wraps midnight
    assert slot_category(time(16, 0), slots) is None


def test_distance() -> None:
    assert links.distance_m(1.2799, 103.8443, 1.2799, 103.8443) == 0
    assert 60 < links.distance_m(1.2799, 103.8443, 1.2805, 103.8443) < 75
