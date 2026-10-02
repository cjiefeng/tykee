"""PlaceService (§10.5): turns Maps links and Telegram venue/location messages into ``places``
rows, vault notes (``shared/places/<slug>.md``) and inline ``⟦place: …⟧`` annotations, so the
orchestrator, judge, harvester and import all see the shop name without extra work.

Privacy (§15.4): only named businesses are stored. A link or pin that resolves to coordinates,
an address or a home becomes ``⟦location shared⟧`` and the link itself is dropped from the
stored text.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from aiogram.types import Message

from app.brain import notes as nt
from app.brain.store import NoteStore
from app.db.database import Database
from app.decisions.text import normalise
from app.places import links
from app.places.links import LOCATION_SHARED, ParsedPlace
from app.places.resolver import PlaceResolver, Resolution
from app.settings import SettingsStore
from app.timeutil import from_sql, to_sql, utcnow

log = logging.getLogger(__name__)

SAME_PLACE_M = 75.0
RETRY_AFTER = timedelta(hours=6)
MAX_ATTEMPTS = 2  # the first try plus one retry (§10.5 failure handling)
DETAILS = "Details"


@dataclass(frozen=True)
class Candidate:
    name: str
    maps_url: str
    address: str | None = None
    lat: float | None = None
    lng: float | None = None
    google_id: str | None = None


@dataclass(frozen=True)
class Place:
    id: int
    name: str
    google_id: str | None
    lat: float | None
    lng: float | None
    address: str | None
    maps_url: str
    note_path: str | None
    first_seen_at: str
    last_seen_at: str
    visit_count: int

    def marker(self) -> str:
        return links.annotation(
            self.name, address=self.address, lat=self.lat, lng=self.lng, place_id=self.id
        )


@dataclass(frozen=True)
class LinkRow:
    url: str
    status: str
    error: str | None
    attempts: int
    resolved_at: str
    place_id: int | None
    place_name: str | None


@dataclass
class AnnotatedText:
    text: str
    places: list[Place] = field(default_factory=list)
    unnamed: int = 0


def _place(r: sqlite3.Row) -> Place:
    return Place(
        r["id"],
        r["name"],
        r["google_id"],
        r["lat"],
        r["lng"],
        r["address"],
        r["maps_url"],
        r["note_path"],
        r["first_seen_at"],
        r["last_seen_at"],
        r["visit_count"],
    )


def candidate_from(parsed: ParsedPlace, maps_url: str) -> Candidate:
    assert parsed.name is not None
    return Candidate(
        parsed.name, maps_url, parsed.address, parsed.lat, parsed.lng, parsed.google_id
    )


def venue_url(name: str, address: str | None, google_place_id: str | None) -> str:
    """Maps search URL for a Telegram venue (which carries no link of its own)."""
    query = f"{name}, {address}" if address else name
    params = {"api": "1", "query": query}
    if google_place_id:
        params["query_place_id"] = google_place_id
    return "https://www.google.com/maps/search/?" + urlencode(params)


def _same(r: sqlite3.Row, cand: Candidate) -> bool:
    ids = r["google_id"], cand.google_id
    if ids[0] and ids[1] and ids[0].split(":")[0] == ids[1].split(":")[0] and ids[0] != ids[1]:
        return False  # two different places Google knows about (same id scheme, other id)
    if None not in (r["lat"], r["lng"], cand.lat, cand.lng):
        return links.distance_m(r["lat"], r["lng"], cand.lat, cand.lng) <= SAME_PLACE_M  # type: ignore[arg-type]
    return True  # same name, no coordinates to tell them apart


def _upsert(c: sqlite3.Connection, cand: Candidate, now: str) -> tuple[Place, str | None]:
    """Returns the place and its previous ``last_seen_at`` (None when it was just created)."""
    norm = normalise(cand.name)
    row = None
    if cand.google_id:
        row = c.execute("SELECT * FROM places WHERE google_id = ?", (cand.google_id,)).fetchone()
    if row is None:
        same_name = c.execute("SELECT * FROM places WHERE name_norm = ?", (norm,)).fetchall()
        row = next((r for r in same_name if _same(r, cand)), None)
    if row is None:
        cur = c.execute(
            "INSERT INTO places(name, name_norm, google_id, lat, lng, address, maps_url, "
            "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cand.name,
                norm,
                cand.google_id,
                cand.lat,
                cand.lng,
                cand.address,
                cand.maps_url,
                now,
                now,
            ),
        )
        new = c.execute("SELECT * FROM places WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _place(new), None
    c.execute(
        "UPDATE places SET last_seen_at = ?, lat = COALESCE(lat, ?), lng = COALESCE(lng, ?), "
        "address = COALESCE(address, ?) WHERE id = ?",
        (now, cand.lat, cand.lng, cand.address, row["id"]),
    )
    if cand.google_id and row["google_id"] is None:
        c.execute(
            "UPDATE OR IGNORE places SET google_id = ? WHERE id = ?", (cand.google_id, row["id"])
        )
    fresh = c.execute("SELECT * FROM places WHERE id = ?", (row["id"],)).fetchone()
    return _place(fresh), row["last_seen_at"]


def _rewrite_stored(c: sqlite3.Connection, url: str, replacement: str) -> int:
    """Patch user messages already stored with ``url`` (retry job): annotate, or drop the link
    when it turned out to be a home/address."""
    rows = c.execute(
        "SELECT id, content FROM messages WHERE role = 'user' AND instr(content, ?) > 0 "
        "AND instr(content, ? || ' ⟦') = 0",
        (url, url),
    ).fetchall()
    for r in rows:
        blocks = json.loads(r["content"])
        for b in blocks:
            if b.get("type") == "text":
                b["text"] = str(b.get("text", "")).replace(url, replacement, 1)
        c.execute(
            "UPDATE messages SET content = ? WHERE id = ?",
            (json.dumps(blocks, ensure_ascii=False), r["id"]),
        )
    return len(rows)


# Lines of the Details section that _sync_note owns; anything else in a place note is kept.
GENERATED = (
    "- Google Maps:",
    "- Address:",
    "- Coordinates:",
    "- First mentioned:",
    "- Last mentioned:",
    "- Visits recorded:",
)


def _is_details(line: str) -> bool:
    return line.startswith("## ") and line[3:].strip().casefold() == DETAILS.casefold()


def details_extras(body: str) -> list[str]:
    """Lines in the Details section that weren't generated, e.g. something appended to the note
    without a heading (which lands at the end, inside Details)."""
    out: list[str] = []
    inside = False
    for line in body.splitlines():
        if line.startswith(("# ", "## ")):
            inside = _is_details(line)
            continue
        if inside and line.strip() and not line.startswith(GENERATED):
            out.append(line)
    return out


def own_content(body: str) -> str:
    """A note's content without its title and generated lines (merging places)."""
    lines = body.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    kept = [ln for ln in lines if not _is_details(ln) and not ln.startswith(GENERATED)]
    return "\n".join(kept).strip()


class PlaceService:
    def __init__(
        self,
        *,
        db: Database,
        settings: SettingsStore,
        resolver: PlaceResolver,
        tz: ZoneInfo,
        store: NoteStore | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._settings = settings
        self.resolver = resolver
        self._tz = tz
        self._store = store
        self._clock = clock

    # --- incoming messages -------------------------------------------------------------------

    async def annotate(self, msg: Message, text: str) -> AnnotatedText:
        """Called for every persisted message, before it is stored (§10.5 detection)."""
        s = await self._settings.load()
        if not s.places_enabled:
            return AnnotatedText(text)
        if msg.venue is not None:
            v = msg.venue
            if not links.is_named_business(v.title):
                return AnnotatedText(f"[location] {LOCATION_SHARED}", unnamed=1)
            place = await self.upsert(
                Candidate(
                    v.title,
                    venue_url(v.title, v.address, v.google_place_id),
                    v.address or None,
                    v.location.latitude,
                    v.location.longitude,
                    f"gpid:{v.google_place_id}" if v.google_place_id else None,
                )
            )
            return AnnotatedText(f"{text} {place.marker()}", [place])
        if msg.location is not None:
            return AnnotatedText(f"[location] {LOCATION_SHARED}", unnamed=1)
        body = msg.text or msg.caption or ""
        urls = links.urls_in_entities(body, msg.entities or msg.caption_entities or [])
        return await self.annotate_urls(text, urls)

    async def annotate_urls(self, text: str, urls: Sequence[tuple[str, bool]]) -> AnnotatedText:
        out = AnnotatedText(text)
        for url, visible in urls:
            res = await self.resolver.resolve(url)
            if res.status == "resolved" and res.parsed is not None:
                place = await self.upsert(candidate_from(res.parsed, url), url=url)
                out.places.append(place)
                if visible and url in out.text:
                    out.text = out.text.replace(url, f"{url} {place.marker()}", 1)
                else:
                    out.text = f"{out.text} {place.marker()}"
            elif res.status == "unnamed":
                out.unnamed += 1
                if visible and url in out.text:
                    out.text = out.text.replace(url, LOCATION_SHARED, 1)
                else:
                    out.text = f"{out.text} {LOCATION_SHARED}"
        if out.places or out.unnamed:
            log.info("places in message", extra={"places": len(out.places), "unnamed": out.unnamed})
        return out

    # --- places ------------------------------------------------------------------------------

    async def upsert(self, cand: Candidate, *, url: str | None = None) -> Place:
        """Dedupe by Google id, else the same normalised name within 75 m; then keep the vault
        note in step. ``url`` links the resolver cache row to the place."""
        now = to_sql(self._clock())

        def _go(c: sqlite3.Connection) -> tuple[Place, str | None]:
            result = _upsert(c, cand, now)
            if url is not None:
                c.execute("UPDATE place_links SET place_id = ? WHERE url = ?", (result[0].id, url))
            return result

        place, previous = await self._db.write(_go)
        if previous is None:
            log.info("place created", extra={"place_id": place.id})
        if previous is None or self._day(previous) != self._day(place.last_seen_at):
            place = await self._sync_note(place)
        return place

    def _day(self, sql_ts: str) -> str:
        return from_sql(sql_ts).astimezone(self._tz).date().isoformat()

    async def get(self, place_id: int) -> Place | None:
        row = await self._db.read(
            lambda c: c.execute("SELECT * FROM places WHERE id = ?", (place_id,)).fetchone()
        )
        return _place(row) if row else None

    async def all(self) -> list[Place]:
        rows = await self._db.read(
            lambda c: c.execute("SELECT * FROM places ORDER BY last_seen_at DESC, id").fetchall()
        )
        return [_place(r) for r in rows]

    async def by_name(self, name: str) -> list[Place]:
        norm = normalise(name)
        rows = await self._db.read(
            lambda c: c.execute("SELECT * FROM places WHERE name_norm = ?", (norm,)).fetchall()
        )
        return [_place(r) for r in rows]

    async def recent_links(self, limit: int = 30) -> list[LinkRow]:
        rows = await self._db.read(
            lambda c: c.execute(
                "SELECT l.*, p.name AS place_name FROM place_links l "
                "LEFT JOIN places p ON p.id = l.place_id ORDER BY l.resolved_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        )
        return [
            LinkRow(
                r["url"],
                r["status"],
                r["error"],
                r["attempts"],
                r["resolved_at"],
                r["place_id"],
                r["place_name"],
            )
            for r in rows
        ]

    async def visit(self, place_id: int) -> Place | None:
        """A decision for this place was recorded: count it and refresh the note."""
        now = to_sql(self._clock())
        await self._db.write(
            lambda c: c.execute(
                "UPDATE places SET visit_count = visit_count + 1, last_seen_at = ? WHERE id = ?",
                (now, place_id),
            )
        )
        place = await self.get(place_id)
        return await self._sync_note(place) if place is not None else None

    async def adopt(self, cand: Candidate, url: str) -> Place | None:
        """Import (§10.5): a link resolved before extraction becomes a place only when a reviewed
        and applied option or decision names it. Links them up and counts imported visits."""
        from rapidfuzz import fuzz

        want = normalise(cand.name)

        def same(name: str) -> bool:
            have = normalise(name)
            return have == want or fuzz.ratio(have, want) >= 90

        def _matches(c: sqlite3.Connection) -> tuple[list[int], list[int]]:
            opts = [
                int(r[0])
                for r in c.execute("SELECT id, name FROM options WHERE place_id IS NULL")
                if same(r[1])
            ]
            decs = [
                int(r[0])
                for r in c.execute(
                    "SELECT id, choice_text FROM decisions WHERE place_id IS NULL "
                    "AND source = 'import' AND status = 'accepted'"
                )
                if same(r[1])
            ]
            return opts, decs

        opts, decs = await self._db.read(_matches)
        if not opts and not decs:
            return None
        place = await self.upsert(cand, url=url)

        def _link(c: sqlite3.Connection) -> None:
            c.executemany(
                "UPDATE options SET place_id = ? WHERE id = ?", [(place.id, i) for i in opts]
            )
            c.executemany(
                "UPDATE decisions SET place_id = ? WHERE id = ?", [(place.id, i) for i in decs]
            )
            c.execute(
                "UPDATE places SET visit_count = visit_count + ? WHERE id = ?",
                (len(decs), place.id),
            )

        await self._db.write(_link)
        linked = await self.get(place.id)
        return await self._sync_note(linked) if linked is not None else place

    # --- dashboard (§11 Memory → Places) -----------------------------------------------------

    async def rename(self, place_id: int, name: str) -> Place:
        name = " ".join(name.split())
        if not name:
            raise ValueError("name can't be empty")
        place = await self.get(place_id)
        if place is None:
            raise ValueError(f"no place {place_id}")

        def _go(c: sqlite3.Connection) -> None:
            c.execute(
                "UPDATE places SET name = ?, name_norm = ? WHERE id = ?",
                (name, normalise(name), place_id),
            )
            # Options named after the place follow it, unless that name is taken in the category.
            c.execute("UPDATE OR IGNORE options SET name = ? WHERE place_id = ?", (name, place_id))

        await self._db.write(_go)
        await self._retitle_note(place, name)
        renamed = await self.get(place_id)
        assert renamed is not None
        return await self._sync_note(renamed)

    async def merge(self, src_id: int, dst_id: int) -> Place:
        """Fold a duplicate into ``dst``: options, decisions and links move over, visits add up,
        and anything learned in the source's note is appended to the destination's."""
        if src_id == dst_id:
            raise ValueError("can't merge a place into itself")
        src, dst = await self.get(src_id), await self.get(dst_id)
        if src is None or dst is None:
            raise ValueError("unknown place")

        def _go(c: sqlite3.Connection) -> None:
            for table in ("options", "decisions", "place_links"):
                c.execute(f"UPDATE {table} SET place_id = ? WHERE place_id = ?", (dst_id, src_id))
            c.execute("DELETE FROM places WHERE id = ?", (src_id,))
            c.execute(
                "UPDATE places SET visit_count = visit_count + ?, "
                "first_seen_at = min(first_seen_at, ?), last_seen_at = max(last_seen_at, ?), "
                "google_id = COALESCE(google_id, ?), lat = COALESCE(lat, ?), "
                "lng = COALESCE(lng, ?), address = COALESCE(address, ?) WHERE id = ?",
                (
                    src.visit_count,
                    src.first_seen_at,
                    src.last_seen_at,
                    src.google_id,
                    src.lat,
                    src.lng,
                    src.address,
                    dst_id,
                ),
            )

        await self._db.write(_go)
        merged = await self.get(dst_id)
        assert merged is not None
        merged = await self._sync_note(merged)
        if self._store is not None and src.note_path and src.note_path != merged.note_path:
            note = await self._store.read(src.note_path)
            extra = own_content(note.body) if note is not None else ""
            if extra and merged.note_path:
                await self._store.write(
                    merged.note_path,
                    mode="append",
                    content=extra,
                    heading=f"From {src.name}",
                    source=f"place:{src_id}",
                )
            await self._store.delete(src.note_path)
        log.info("places merged", extra={"src": src_id, "dst": dst_id})
        return merged

    async def delete(self, place_id: int) -> bool:
        place = await self.get(place_id)
        if place is None:
            return False

        def _go(c: sqlite3.Connection) -> None:
            for table in ("options", "decisions", "place_links"):
                c.execute(f"UPDATE {table} SET place_id = NULL WHERE place_id = ?", (place_id,))
            c.execute("DELETE FROM places WHERE id = ?", (place_id,))

        await self._db.write(_go)
        if self._store is not None and place.note_path:
            await self._store.delete(place.note_path)
        log.info("place deleted", extra={"place_id": place_id})
        return True

    # --- retry (§10.5 failure handling) ------------------------------------------------------

    async def retry_failed(self) -> int:
        """Failed links get one more try once they're ``RETRY_AFTER`` old. Hourly job."""
        s = await self._settings.load()
        if not s.places_enabled:
            return 0
        cutoff = to_sql(self._clock() - RETRY_AFTER)
        urls = await self._db.read(
            lambda c: [
                r[0]
                for r in c.execute(
                    "SELECT url FROM place_links WHERE status = 'failed' AND attempts < ? "
                    "AND resolved_at <= ? ORDER BY resolved_at LIMIT 50",
                    (MAX_ATTEMPTS, cutoff),
                )
            ]
        )
        for url in urls:
            res = await self.resolver.resolve(url, refresh=True)
            await self._settle(res)
        if urls:
            log.info("place links retried", extra={"links": len(urls)})
        return len(urls)

    async def _settle(self, res: Resolution) -> None:
        if res.status == "resolved" and res.parsed is not None:
            place = await self.upsert(candidate_from(res.parsed, res.url), url=res.url)
            replacement = f"{res.url} {place.marker()}"
        elif res.status == "unnamed":
            replacement = LOCATION_SHARED
        else:
            return
        await self._db.write(lambda c: _rewrite_stored(c, res.url, replacement))

    # --- vault -------------------------------------------------------------------------------

    async def _note_path(self, place: Place) -> str:
        if place.note_path:
            return place.note_path
        slug = nt.slug_for_title(place.name)
        base = f"shared/places/{slug}.md"
        taken = await self._db.read(
            lambda c: c.execute(
                "SELECT 1 FROM places WHERE note_path = ? AND id != ?", (base, place.id)
            ).fetchone()
        )
        return f"shared/places/{slug}-{place.id}.md" if taken else base

    async def _retitle_note(self, place: Place, name: str) -> None:
        if self._store is None or not place.note_path:
            return
        note = await self._store.read(place.note_path)
        if note is None:
            return
        lines = note.body.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("# "):
                lines[i] = f"# {name}"
                break
        note.body = "\n".join(lines) + "\n"
        await self._store.write_raw(place.note_path, nt.render(note))

    def _details(self, p: Place) -> str:
        lines = [f"- Google Maps: [{p.name}]({p.maps_url})"]
        if p.address:
            lines.append(f"- Address: {p.address}")
        if p.lat is not None and p.lng is not None:
            lines.append(f"- Coordinates: {p.lat:.5f}, {p.lng:.5f}")
        lines.append(f"- First mentioned: {self._day(p.first_seen_at)}")
        lines.append(f"- Last mentioned: {self._day(p.last_seen_at)}")
        lines.append(f"- Visits recorded: {p.visit_count}")
        return "\n".join(lines)

    async def _sync_note(self, place: Place) -> Place:
        """Create the place note, or rewrite only its Details section (anything learned about
        the place elsewhere in the note is kept)."""
        if self._store is None:
            return place
        rel = await self._note_path(place)
        try:
            note = await self._store.read(rel)
            if note is not None:
                extras = details_extras(note.body)
                content = self._details(place) + ("\n\n" + "\n".join(extras) if extras else "")
                await self._store.write(
                    rel,
                    mode="replace_section",
                    heading=DETAILS,
                    content=content,
                    source=f"place:{place.id}",
                )
            else:
                await self._store.write(
                    rel,
                    mode="create",
                    content=f"## {DETAILS}\n\n{self._details(place)}",
                    title=place.name,
                    note_type="place",
                    tags=["place"],
                    source=f"place:{place.id}",
                )
        except Exception:
            # The DB row is what decisions use; a vault hiccup must not break the message.
            log.exception("place note write failed", extra={"place_id": place.id})
            return place
        if place.note_path != rel:
            await self._db.write(
                lambda c: c.execute("UPDATE places SET note_path = ? WHERE id = ?", (rel, place.id))
            )
        return replace(place, note_path=rel)
