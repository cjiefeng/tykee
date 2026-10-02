"""Place recommendations (§10.6): "brunch around Tiong Bahru, pet friendly".

Code does the locating, filtering, ranking and the weighted random pick; Claude only composes
the reply and, when ``find_places`` says so, searches the web for new places and hands them to
``save_place_candidates``. No Google Places API.

score = distance_fit * liked * recency * trust (pure functions below), then ``n`` picks by
weighted random from the top ~8 without replacement, with ``recommend.explore_ratio`` of the
picks reserved for a place they haven't been to. Every pick is a ``decisions`` row (status
'suggested', ``place_id`` set, ``context_json = {"recommend": …}``), so ✅ works like any pick
and places already shown in this chat aren't shown again within ``decisions.session_hours``.
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from app.db.database import Database
from app.decisions import categories as cats
from app.decisions.categories import Category
from app.decisions.engine import option_prefs, recency_factor, sample
from app.places import areas as area_db
from app.places import attributes as attrs
from app.places.areas import Area
from app.places.attributes import Attribute
from app.places.links import distance_m
from app.places.service import Place, PlaceService, _place
from app.settings import RuntimeSettings, SettingsStore
from app.timeutil import from_sql, to_sql, utcnow

log = logging.getLogger(__name__)

TOP_K = 8
TEXT_MATCH_FIT = 0.7
EDGE_FIT = 0.3
TRUST_USER, TRUST_WEB, TRUST_MAYBE = 1.0, 0.8, 0.6
_rng: random.Random = random.SystemRandom()


# --- requests --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Centre:
    label: str  # what to call it in replies: 'Tiong Bahru', 'Merci Marcel'
    lat: float
    lng: float
    radius_m: int
    area_id: int | None = None  # a gazetteer area: text-matched places count as inside
    area_name: str | None = None


@dataclass(frozen=True)
class RecommendRequest:
    """Stored as ``decisions.context_json`` under "recommend", so 🎲 more can re-run it."""

    category_id: int
    for_users: str
    centre: Centre
    must: list[str] = field(default_factory=list)
    n: int = 3
    anchor_place_id: int | None = None

    def to_json(self) -> str:
        return json.dumps({"recommend": asdict(self)}, ensure_ascii=False)

    @classmethod
    def from_context(cls, raw: str | None) -> RecommendRequest | None:
        try:
            d = json.loads(raw or "")["recommend"]
            return cls(**{**d, "centre": Centre(**d["centre"])})
        except (ValueError, KeyError, TypeError):
            return None


# --- pure ranking ----------------------------------------------------------------------------


def distance_fit(distance_m: float, radius_m: float) -> float | None:
    """1.0 within radius/2, falling linearly to 0.3 at the edge; None outside."""
    if distance_m > radius_m:
        return None
    half = radius_m / 2
    if distance_m <= half:
        return 1.0
    return 1.0 - (1.0 - EDGE_FIT) * (distance_m - half) / half


@dataclass(frozen=True)
class MustCheck:
    passes: bool  # every must-have is a passing value
    maybe: bool  # some must-have is unknown (or stale); none is a 'no'
    trust: float
    shown: dict[str, Attribute]  # the must-have attributes found, for labels


def check_must(
    found: Mapping[str, Attribute], must: Sequence[str], now: datetime, ttl_days: int
) -> MustCheck | None:
    """None: excluded (a must-have is 'no'). ``unknown`` places are only 'maybe' (§10.6)."""
    trust, maybe = TRUST_USER, False
    shown: dict[str, Attribute] = {}
    for key in must:
        a = found.get(key)
        value = a.effective(now, ttl_days) if a else "unknown"
        if a is not None:
            shown[key] = a
        if value == "no":
            return None
        if value not in attrs.PASSES[key]:
            maybe = True
        elif a is not None and a.source == "web":
            trust = min(trust, TRUST_WEB)
    if maybe:
        trust = TRUST_MAYBE
    return MustCheck(not maybe, maybe, trust, shown)


@dataclass(frozen=True)
class Scored:
    place: Place
    option_id: int | None
    fit: float
    liked: float
    recency: float
    trust: float
    last_visit: str | None  # UTC SQL time of the last accepted decision for the place
    must: MustCheck

    @property
    def score(self) -> float:
        return self.fit * self.liked * self.recency * self.trust

    @property
    def new(self) -> bool:
        return self.place.status == "unvisited" or (
            self.place.visit_count == 0 and self.last_visit is None
        )


def choose(
    scored: Sequence[Scored], n: int, explore: int, rng: random.Random, *, backfill: bool = True
) -> tuple[list[Scored], int]:
    """Weighted random picks: ``explore`` slots from new places first, the rest from the top
    ``TOP_K`` by score. Explore slots no new place can fill go to known places when
    ``backfill``, else stay empty (for a web find). Returns (picks, unfilled explore slots)."""
    ranked = sorted(scored, key=lambda s: -s.score)
    new = [s for s in ranked if s.new]
    explore = min(explore, n)
    explored = [new[i] for i in sample([s.score for s in new], explore, rng)]
    rest = [s for s in ranked if s not in explored]
    top = rest[: max(TOP_K, n)]
    unfilled = explore - len(explored)
    want = n - len(explored) - (0 if backfill else unfilled)
    known = [top[i] for i in sample([s.score for s in top], want, rng)]
    picks = sorted(known, key=lambda s: -s.score) + explored
    return picks, unfilled


def ago(then: datetime, now: datetime) -> str:
    days = (now - then).total_seconds() / 86400
    if days < 1:
        return "today"
    if days < 2:
        return "yesterday"
    if days < 14:
        return f"{int(days)} days ago"
    if days < 60:
        return f"{int(days // 7)} weeks ago"
    return f"{int(days // 30)} months ago"


# --- results ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Shown:
    number: int
    decision_id: int
    item: Scored


@dataclass
class FindResult:
    request: RecommendRequest
    picks: list[Shown]
    considered: int
    suggest_web: bool = False
    slots_left: int = 0  # picks still wanted from the web
    maybe: list[Scored] = field(default_factory=list)  # unconfirmed must-haves, held back


@dataclass
class TurnState:
    """What a turn's find_places left for save_place_candidates."""

    result: FindResult
    category: Category


class RecommendError(ValueError):
    pass


Constraints = Callable[[str], Awaitable[set[str]]]


class RecommendService:
    def __init__(
        self,
        *,
        db: Database,
        settings: SettingsStore,
        places: PlaceService,
        users_by_slug: Mapping[str, int],
        clock: Callable[[], datetime] = utcnow,
        rng: random.Random | None = None,
        constraints: Constraints | None = None,
    ) -> None:
        self._db = db
        self._settings = settings
        self.places = places
        self._users = dict(users_by_slug)
        self._clock = clock
        self._rng = rng or _rng
        self._constraints = constraints

    async def settings(self) -> RuntimeSettings:
        return await self._settings.load()

    # --- locating (step 1) -------------------------------------------------------------------

    async def locate(
        self,
        *,
        area: str | None = None,
        near_maps_url: str | None = None,
        anchor_place_id: int | None = None,
    ) -> Centre:
        s = await self._settings.load()
        if anchor_place_id is not None:
            place = await self.places.get(anchor_place_id)
            if place is None:
                raise RecommendError(f"unknown place_id {anchor_place_id}")
            if place.lat is not None and place.lng is not None:
                return Centre(place.name, place.lat, place.lng, s.recommend_default_radius_m)
            a = await self._area(place.area_id) if place.area_id else None
            if a is None:
                raise RecommendError(f"I don't know where {place.name} is; ask for an area")
            return Centre(place.name, a.lat, a.lng, a.radius_m, a.id, a.name)
        if near_maps_url:
            res = await self.places.resolver.resolve(near_maps_url)
            p = res.parsed
            if res.status != "resolved" or p is None or p.lat is None or p.lng is None:
                # Unnamed links are homes or bare addresses: never used (§10.5 privacy rule).
                raise RecommendError("couldn't place that link; ask which area they mean")
            return Centre(p.name or "there", p.lat, p.lng, s.recommend_default_radius_m)
        if not area or not area.strip():
            raise RecommendError("give an area, near_maps_url or anchor_place_id")
        found = await self._db.read(lambda c: area_db.match(c, area))
        if found is None:
            raise RecommendError(
                f"unknown area {area!r}. Ask which neighbourhood or MRT station they mean; for "
                "'near home' a home area has to be set in the dashboard first"
            )
        return Centre(found.name, found.lat, found.lng, found.radius_m, found.id, found.name)

    async def _area(self, area_id: int) -> Area | None:
        return await self._db.read(lambda c: area_db.get(c, area_id))

    # --- finding known places (steps 2 + 4) --------------------------------------------------

    async def find(
        self,
        category: Category,
        centre: Centre,
        *,
        for_users: str,
        must: Sequence[str],
        n: int | None,
        asked_by: int,
        chat_id: int | None,
        web: bool,
        anchor_place_id: int | None = None,
    ) -> FindResult:
        """``web``: the web tools are available this turn, so explore slots that known places
        can't fill are left for discovery instead of being filled with known places."""
        s = await self._settings.load()
        must = list(dict.fromkeys(k.strip().casefold() for k in must))
        for key in must:
            if key not in attrs.VALUES:
                raise RecommendError(f"unknown must-have {key!r}; one of {sorted(attrs.VALUES)}")
        req = RecommendRequest(
            category.id,
            for_users,
            centre,
            must,
            min(max(n or s.recommend_default_n, 1), 5),
            anchor_place_id,
        )
        return await self._run(req, category, s, asked_by=asked_by, chat_id=chat_id, web=web)

    async def more(self, decision_id: int, *, asked_by: int, chat_id: int | None) -> FindResult:
        """🎲 more: the same request again, known places only, never repeating one shown."""
        row = await self._db.read(
            lambda c: c.execute(
                "SELECT category_id, context_json FROM decisions WHERE id = ?", (decision_id,)
            ).fetchone()
        )
        req = RecommendRequest.from_context(row["context_json"]) if row else None
        if req is None:
            raise RecommendError("that recommendation has expired")
        category = await self._db.read(lambda c: cats.get_by_id(c, req.category_id))
        if category is None:
            raise RecommendError("that category is gone")
        s = await self._settings.load()
        return await self._run(req, category, s, asked_by=asked_by, chat_id=chat_id, web=False)

    async def _run(
        self,
        req: RecommendRequest,
        category: Category,
        s: RuntimeSettings,
        *,
        asked_by: int,
        chat_id: int | None,
        web: bool,
    ) -> FindResult:
        now = self._clock()
        hard = await self._constraints(req.for_users) if self._constraints else set()
        since = to_sql(now - timedelta(hours=s.decisions_session_hours))
        pref_users = (
            list(self._users.values()) if req.for_users == "both" else [self._users[req.for_users]]
        )

        def _go(c: sqlite3.Connection) -> FindResult:
            scored = _pool(c, req, category, s, now, since, chat_id, pref_users, hard)
            main = [x for x in scored if x.must.passes]
            maybe = sorted((x for x in scored if x.must.maybe), key=lambda x: -x.score)
            explore = round(req.n * s.recommend_explore_ratio)
            picks, unfilled = choose(main, req.n, explore, self._rng, backfill=not web)
            suggest_web = len(main) < req.n or unfilled > 0
            if not web and len(picks) < req.n:
                k = req.n - len(picks)
                picks, maybe = picks + maybe[:k], maybe[k:]  # "maybe, couldn't confirm"
            shown = _insert(c, req, picks, asked_by=asked_by, chat_id=chat_id, now=now)
            return FindResult(
                req,
                shown,
                considered=len(scored),
                suggest_web=suggest_web,
                slots_left=req.n - len(shown),
                maybe=maybe,
            )

        result = await self._db.write(_go)
        log.info(
            "places found",
            extra={
                "category": category.slug,
                "considered": result.considered,
                "picks": len(result.picks),
                "web": result.suggest_web,
            },
        )
        return result

    # --- web finds (step 5) ------------------------------------------------------------------

    async def save_found(
        self,
        state: TurnState,
        candidates: Sequence[FoundCandidate],
        *,
        asked_by: int,
        chat_id: int | None,
    ) -> tuple[list[Shown], list[str]]:
        """Store web finds as 'unvisited' places with web-sourced attributes, then fill the
        request's remaining slots: confirmed finds first, then unconfirmed ones, then known
        'maybe' places. Returns (new picks, notes about skipped candidates)."""
        s = await self._settings.load()
        req, now = state.result.request, self._clock()
        skipped: list[str] = []
        centre_area = await self._area(req.centre.area_id) if req.centre.area_id else None
        if centre_area is not None and centre_area.source == "user":
            centre_area = None  # a user area ("home") is never a place's area
        stored: list[Place] = []
        for cand in candidates:
            texts = [t for t in (cand.address, cand.area) if t]

            def _where(c: sqlite3.Connection, texts: list[str] = texts) -> Area | None:
                return next((a for t in texts if (a := area_db.in_text(c, t))), None)

            area = await self._db.read(_where)
            place, _ = await self.places.add_found(
                cand.name,
                area=area or centre_area,
                address=cand.address,
                category_id=req.category_id,
            )
            for a in cand.attributes:
                evidence = " ".join(x for x in (a.quote, cand.source_url) if x)
                await self.places.set_attribute(
                    place.id, a.key, a.value, source="web", evidence=evidence
                )
            stored.append(place)

        since = to_sql(now - timedelta(hours=s.decisions_session_hours))
        taken = {sh.item.place.id for sh in state.result.picks}

        def _go(c: sqlite3.Connection) -> list[Shown]:
            seen = _shown_ids(c, req.category_id, chat_id, since) | taken
            ttl = s.recommend_web_attr_ttl_days
            found = attrs.of(c, [p.id for p in stored])
            good: list[Scored] = []
            unsure: list[Scored] = []
            for p in stored:
                fresh = _place(c.execute("SELECT * FROM places WHERE id = ?", (p.id,)).fetchone())
                if p.id in seen:
                    skipped.append(f"{p.name}: already suggested")
                    continue
                located = fresh.lat is not None and fresh.lng is not None
                if located and distance_fit(_dist(fresh, req.centre), req.centre.radius_m) is None:
                    skipped.append(f"{p.name}: outside the area")
                    continue
                check = check_must(found.get(p.id, {}), req.must, now, ttl)
                if check is None:
                    skipped.append(f"{p.name}: doesn't meet the must-haves")
                    continue
                item = Scored(fresh, None, TEXT_MATCH_FIT, 1.0, 1.0, check.trust, None, check)
                (good if check.passes else unsure).append(item)
                seen.add(p.id)
            pool = [*good, *unsure, *state.result.maybe]
            slots = req.n - len(state.result.picks)
            first = len(state.result.picks) + 1
            return _insert(
                c, req, pool[:slots], asked_by=asked_by, chat_id=chat_id, now=now, first=first
            )

        shown = await self._db.write(_go)
        state.result.picks += shown
        state.result.slots_left = req.n - len(state.result.picks)
        state.result.maybe = [m for m in state.result.maybe if m not in [x.item for x in shown]]
        return shown, skipped

    # --- labels ------------------------------------------------------------------------------

    async def describe(
        self, shown: Shown, must: Sequence[str], pet_emoji: str
    ) -> dict[str, object]:
        """One pick for Claude (or the 🎲 more message): facts to put on one line."""
        s = await self._settings.load()
        now = self._clock()
        x = shown.item
        out: dict[str, object] = {"n": shown.number, "place_id": x.place.id, "name": x.place.name}
        if x.new:
            out["new_to_you"] = True
        elif x.last_visit:
            out["last_went"] = ago(from_sql(x.last_visit), now)
        if x.place.visit_count:
            out["visits"] = x.place.visit_count
        if x.liked > 1.05:
            out["liked"] = "rated up before"
        elif x.liked < 0.95:
            out["liked"] = "rated down before"
        labels: dict[str, str] = {}
        for key in must:
            a = x.must.shown.get(key)
            text = a.label(now, s.recommend_web_attr_ttl_days) if a else "couldn't confirm"
            labels[key] = f"{pet_emoji} {text}" if key == "pet_friendly" else text
        if labels:
            out["must_haves"] = labels
        if x.must.maybe:
            out["maybe"] = True
        out["maps_url"] = x.place.maps_url
        out["line"] = line(out)
        return out


def line(d: Mapping[str, object]) -> str:
    """'2. **Ottomani**: last went 6 weeks ago · 🐶 pets OK (per …) · [Map](…)'."""
    bits: list[str] = []
    if d.get("new_to_you"):
        bits.append("🆕 new to you")
    elif d.get("last_went"):
        bits.append(f"last went {d['last_went']}")
    if d.get("liked"):
        bits.append(str(d["liked"]))
    must = d.get("must_haves")
    if isinstance(must, dict):
        bits += [str(v) for v in must.values()]
    bits.append(f"[Map]({d['maps_url']})")
    return f"{d['n']}. **{d['name']}**: " + " · ".join(bits)


# --- web finds input -------------------------------------------------------------------------


@dataclass(frozen=True)
class FoundAttribute:
    key: str
    value: str
    quote: str = ""


@dataclass(frozen=True)
class FoundCandidate:
    name: str
    source_url: str
    address: str | None = None
    area: str | None = None
    attributes: list[FoundAttribute] = field(default_factory=list)


# --- DB helpers ------------------------------------------------------------------------------


def _dist(p: Place, centre: Centre) -> float:
    assert p.lat is not None and p.lng is not None
    return distance_m(p.lat, p.lng, centre.lat, centre.lng)


def _shown_ids(
    conn: sqlite3.Connection, category_id: int, chat_id: int | None, since: str
) -> set[int]:
    return {
        int(r[0])
        for r in conn.execute(
            "SELECT DISTINCT place_id FROM decisions WHERE category_id = ? AND chat_id IS ? "
            "AND place_id IS NOT NULL AND created_at >= ?",
            (category_id, chat_id, since),
        )
    }


def _pool(
    conn: sqlite3.Connection,
    req: RecommendRequest,
    category: Category,
    s: RuntimeSettings,
    now: datetime,
    since: str,
    chat_id: int | None,
    pref_users: Sequence[int],
    hard: set[str],
) -> list[Scored]:
    """Known places for this category inside the area, minus the ones already shown, scored."""
    rows = conn.execute(
        "SELECT p.*, o.id AS option_id, o.active AS option_active, o.tags_json FROM places p "
        "LEFT JOIN options o ON o.place_id = p.id AND o.category_id = :cat "
        "WHERE o.id IS NOT NULL OR p.category_hint = :cat OR EXISTS (SELECT 1 FROM decisions d "
        "WHERE d.place_id = p.id AND d.category_id = :cat AND d.status = 'accepted')",
        {"cat": category.id},
    ).fetchall()
    skip = _shown_ids(conn, category.id, chat_id, since)
    if req.anchor_place_id is not None:
        skip.add(req.anchor_place_id)
    area_name = (req.centre.area_name or "").casefold()
    inside: list[tuple[Place, int | None, float]] = []
    for r in rows:
        if r["id"] in skip or r["option_active"] == 0:
            continue
        if r["tags_json"] and hard & {t.casefold() for t in json.loads(r["tags_json"])}:
            continue
        p = _place(r)
        if p.lat is not None and p.lng is not None:
            fit = distance_fit(_dist(p, req.centre), req.centre.radius_m)
        elif (req.centre.area_id is not None and p.area_id == req.centre.area_id) or (
            area_name and area_name in (p.address or "").casefold()
        ):
            fit = TEXT_MATCH_FIT
        else:
            fit = None
        if fit is not None:
            inside.append((p, r["option_id"], fit))
    if not inside:
        return []
    ids = [p.id for p, _, _ in inside]
    found = attrs.of(conn, ids)
    last = dict(
        conn.execute(
            f"SELECT place_id, MAX(created_at) FROM decisions WHERE status = 'accepted' "
            f"AND place_id IN ({','.join('?' * len(ids))}) GROUP BY place_id",
            ids,
        ).fetchall()
    )
    prefs = option_prefs(conn, [o for _, o, _ in inside if o is not None], pref_users)
    out: list[Scored] = []
    for p, option_id, fit in inside:
        check = check_must(found.get(p.id, {}), req.must, now, s.recommend_web_attr_ttl_days)
        if check is None:
            continue
        t = last.get(p.id)
        days = (now - from_sql(t)).total_seconds() / 86400 if t else None
        rec = recency_factor(days, category.recency_tau_days)
        liked = prefs.get(option_id, 1.0) if option_id is not None else 1.0
        out.append(Scored(p, option_id, fit, liked, rec, check.trust, t, check))
    return out


def _insert(
    conn: sqlite3.Connection,
    req: RecommendRequest,
    picks: Sequence[Scored],
    *,
    asked_by: int,
    chat_id: int | None,
    now: datetime,
    first: int = 1,
) -> list[Shown]:
    context = req.to_json()
    out: list[Shown] = []
    for i, x in enumerate(picks):
        cur = conn.execute(
            "INSERT INTO decisions(category_id, option_id, choice_text, for_users, asked_by, "
            "status, chat_id, context_json, created_at, place_id) "
            "VALUES (?, ?, ?, ?, ?, 'suggested', ?, ?, ?, ?)",
            (
                req.category_id,
                x.option_id,
                x.place.name,
                req.for_users,
                asked_by,
                chat_id,
                context,
                to_sql(now),
                x.place.id,
            ),
        )
        out.append(Shown(first + i, int(cur.lastrowid or 0), x))
    return out
