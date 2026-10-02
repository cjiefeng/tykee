"""CONSOLIDATE (§15.3), the deterministic parts: episode de-duplication, validation of the
Opus-designed category set (§15.3.2 step 4), option normalisation and suggested weights,
decisions from chosen episodes, fact filtering, and validation of the merged notes. Pure
functions; the service does the calls and the DB."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import fmean
from typing import Any
from zoneinfo import ZoneInfo

from rapidfuzz import fuzz

from app.brain import notes as nt
from app.decisions.categories import TAU_MAX, TAU_MIN
from app.decisions.text import ALIAS_MAX, display_name_for, normalise, slugify
from app.extraction.schema import Episode, Fact, OptionSeen

SAME_NAME = 90  # rapidfuzz ratio on normalised names (§15.3 step 2)
SAME_FACT = 85
DEDUPE_WINDOW = timedelta(hours=3)
MIN_FACT_CONFIDENCE = 0.6
STANCE_SCORE = {"chosen": 1.0, "proposed": 0.3, "rejected": -0.8}
MAX_QUOTES = 3
MAX_EVIDENCE = 5


def parse_ts(value: str, fallback: datetime, tz: ZoneInfo) -> datetime:
    """A model-supplied ISO time; naive means household-local (the transcript has no offset).
    Unparseable or later than the window's end (hallucinated) → the window's end."""
    try:
        ts = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return fallback
    ts = ts if ts.tzinfo else ts.replace(tzinfo=tz)
    return fallback if ts > fallback else ts


# --- episodes --------------------------------------------------------------------------------


@dataclass
class EpisodeRec:
    id: str  # "w12-e3" (window ordinal, episode index)
    ep: Episode
    ts: datetime

    @property
    def chosen(self) -> bool:
        return self.ep.outcome == "chosen" and bool(self.ep.choice.strip())


def dedupe_episodes(episodes: Iterable[EpisodeRec]) -> list[EpisodeRec]:
    """Overlapping windows can report the same episode twice: same kind of decision, same
    choice, within a few hours → keep the more confident one and pool quotes and phrasings."""
    kept: list[EpisodeRec] = []
    for rec in sorted(episodes, key=lambda r: r.ts):
        key = (normalise(rec.ep.category_phrase), normalise(rec.ep.choice))
        twin = next(
            (
                k
                for k in reversed(kept)
                if rec.ts - k.ts <= DEDUPE_WINDOW
                and (normalise(k.ep.category_phrase), normalise(k.ep.choice)) == key
            ),
            None,
        )
        if twin is None:
            kept.append(rec)
            continue
        best, other = (rec, twin) if rec.ep.confidence > twin.ep.confidence else (twin, rec)
        merged = best.ep.model_copy(
            update={
                "quotes": _unique([*best.ep.quotes, *other.ep.quotes])[:MAX_QUOTES],
                "phrases_seen": _unique([*best.ep.phrases_seen, *other.ep.phrases_seen]),
            }
        )
        kept[kept.index(twin)] = EpisodeRec(best.id, merged, best.ts)
    return kept


def _unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip().casefold()
        if key and key not in seen:
            seen.add(key)
            out.append(item.strip())
    return out


def episode_line(short_id: str, rec: EpisodeRec, tz: ZoneInfo) -> str:
    """One compact line per episode for the category-design call (§15.3.2 step 1)."""
    e = rec.ep
    opts = ", ".join(
        f"{o.name} ({o.by} {o.stance}{': ' + o.reason if o.reason else ''})"
        for o in e.options_considered
    )
    outcome = f"chosen: {e.choice}" if rec.chosen else e.outcome
    return " | ".join(
        [
            short_id,
            f"{rec.ts.astimezone(tz):%Y-%m-%d}",
            e.category_phrase,
            "; ".join(e.phrases_seen),
            e.summary,
            f"options: {opts}" if opts else "options: -",
            outcome,
        ]
    )


# --- categories (§15.3.2) ----------------------------------------------------------------------


@dataclass(frozen=True)
class ExistingCategory:
    id: int
    slug: str
    display_name: str
    description: str


@dataclass
class CategoryProposal:
    slug: str
    display_name: str
    description: str
    recency_tau_days: float
    default_n: int
    allow_generated: bool
    aliases: list[str]
    episode_ids: list[str]
    existing_id: int | None = None
    flags: list[str] = field(default_factory=list)

    def payload(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "display_name": self.display_name,
            "description": self.description,
            "recency_tau_days": self.recency_tau_days,
            "default_n": self.default_n,
            "allow_generated": self.allow_generated,
            "aliases": self.aliases,
            "episode_ids": self.episode_ids,
            "existing_id": self.existing_id,
            "flags": self.flags,
        }


@dataclass
class Design:
    categories: list[CategoryProposal]
    unmapped: list[tuple[str, str]]  # (episode id, why)
    out_of_scope: int = 0

    def category_of(self) -> dict[str, str]:
        return {eid: c.slug for c in self.categories for eid in c.episode_ids}


def validate_design(
    raw: Mapping[str, Any],
    short_ids: Mapping[str, EpisodeRec],
    existing: Sequence[ExistingCategory],
    existing_aliases: Mapping[str, str],
) -> Design:
    """§15.3.2 step 4: unique slugs, every episode mapped exactly once or unmapped, τ clamped,
    aliases normalised, cross-category alias collisions dropped from both and flagged, minimum
    support enforced, out-of-scope episodes discarded. ``existing_aliases`` maps aliases already
    in the DB to their category's slug."""
    by_slug = {c.slug: c for c in existing}
    cats: dict[str, CategoryProposal] = {}
    assigned: set[str] = set()
    unmapped: list[tuple[str, str]] = []
    out_of_scope = 0

    for item in raw.get("unmapped") or []:
        rec = short_ids.get(str(item.get("episode_id", "")))
        if rec is None or rec.id in assigned:
            continue
        assigned.add(rec.id)
        why = str(item.get("why", "")).strip()
        if why == "out_of_scope":
            out_of_scope += 1  # discarded, never shown (§15.4)
        else:
            unmapped.append((rec.id, why or "not mapped"))

    for item in raw.get("categories") or []:
        slug = slugify(str(item.get("slug") or item.get("display_name") or ""))
        if not slug:
            continue
        eps = []
        for sid in item.get("episode_ids") or []:
            rec = short_ids.get(str(sid))
            if rec is not None and rec.id not in assigned:
                assigned.add(rec.id)
                eps.append(rec.id)
        normalised = (normalise(str(x)) for x in item.get("aliases") or [])
        aliases = [a for a in normalised if 0 < len(a) <= ALIAS_MAX]
        if slug in cats:  # duplicate slug: fold into the first
            cats[slug].episode_ids += eps
            cats[slug].aliases = _unique([*cats[slug].aliases, *aliases])
            continue
        prior = by_slug.get(slug)
        cats[slug] = CategoryProposal(
            slug=slug,
            display_name=(
                prior.display_name
                if prior
                else str(item.get("display_name") or "").strip() or display_name_for(slug)
            ),
            description=(
                prior.description if prior else str(item.get("description") or "").strip()
            ),
            recency_tau_days=_clamp(float(item.get("recency_tau_days") or 3.0), TAU_MIN, TAU_MAX),
            default_n=int(_clamp(int(item.get("default_n") or 1), 1, 5)),
            allow_generated=bool(item.get("allow_generated", True)),
            aliases=_unique([normalise(slug.replace("-", " ")), *aliases]),
            episode_ids=eps,
            existing_id=prior.id if prior else None,
        )

    # Minimum support (§15.3.2 rules); a mapping into an existing category needs none.
    recs = {r.id: r for r in short_ids.values()}
    for slug, cat in list(cats.items()):
        chosen = sum(1 for e in cat.episode_ids if recs[e].chosen)
        if cat.existing_id is None and len(cat.episode_ids) < 3 and chosen < 2:
            unmapped += [(e, f"too few episodes for '{slug}'") for e in cat.episode_ids]
            del cats[slug]

    # Aliases: one owner each, and none that already points at another category in the DB.
    owners: dict[str, list[str]] = {}
    for cat in cats.values():
        for a in cat.aliases:
            owners.setdefault(a, []).append(cat.slug)
    for cat in cats.values():
        keep = []
        for a in cat.aliases:
            if len(owners[a]) > 1:
                cat.flags.append(f"alias '{a}' also proposed for {', '.join(owners[a])}; dropped")
            elif a in existing_aliases and existing_aliases[a] != cat.slug:
                cat.flags.append(f"alias '{a}' already belongs to '{existing_aliases[a]}'; dropped")
            else:
                keep.append(a)
        cat.aliases = keep

    for rec in short_ids.values():
        if rec.id not in assigned:
            unmapped.append((rec.id, "not assigned to a category"))
    return Design(list(cats.values()), unmapped, out_of_scope)


def _clamp(v: float, lo: float, hi: float) -> float:
    return min(max(v, lo), hi)


# --- options (§15.3 step 2) ------------------------------------------------------------------


@dataclass
class OptionProposal:
    category_slug: str
    name: str
    tags: list[str] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)
    by_user: dict[str, list[float]] = field(default_factory=dict)
    evidence: list[dict[str, str]] = field(default_factory=list)
    confidence: float = 0.0
    existing: bool = False

    @property
    def mean(self) -> float:
        return fmean(self.scores) if self.scores else 0.0

    @property
    def base_weight(self) -> float:
        return round(_clamp(1.0 + 0.5 * self.mean, 0.5, 1.5), 2)

    def prefs(self) -> dict[str, float]:
        return {
            u: round(_clamp(1.0 + 0.4 * fmean(s), 0.6, 1.4), 2) for u, s in self.by_user.items()
        }

    def payload(self) -> dict[str, Any]:
        return {
            "category_slug": self.category_slug,
            "name": self.name,
            "tags": self.tags,
            "base_weight": self.base_weight,
            "prefs": self.prefs(),
            "mentions": len(self.scores),
            "sentiment": round(self.mean, 2),
            "existing": self.existing,
        }


def _same(a: str, b: str) -> bool:
    return fuzz.ratio(normalise(a), normalise(b)) >= SAME_NAME


class _OptionBook:
    def __init__(self, existing: Mapping[str, Sequence[str]]) -> None:
        self.by_cat: dict[str, list[OptionProposal]] = {
            slug: [OptionProposal(slug, n, existing=True) for n in names]
            for slug, names in existing.items()
        }

    def get(self, slug: str, name: str) -> OptionProposal | None:
        name = name.strip()
        if not name or len(name) > 80:
            return None
        props = self.by_cat.setdefault(slug, [])
        for p in props:
            if _same(p.name, name):
                return p
        p = OptionProposal(slug, name)
        props.append(p)
        return p


def build_options(
    design: Design,
    episodes: Sequence[EpisodeRec],
    seen: Sequence[tuple[OptionSeen, datetime]],
    users: Sequence[str],
    existing: Mapping[str, Sequence[str]],
    tz: ZoneInfo,
) -> list[OptionProposal]:
    """Options per category from episodes (options considered + choices, attributed to who
    raised them) and the extracted option mentions (matched to a category by alias)."""
    category_of = design.category_of()
    alias_to_slug = {a: c.slug for c in design.categories for a in [*c.aliases, c.slug]}
    book = _OptionBook(existing)

    def note(p: OptionProposal, score: float, who: Sequence[str], ts: datetime, quote: str) -> None:
        p.scores.append(score)
        for u in who:
            if u in users:
                p.by_user.setdefault(u, []).append(score)
        entry = {"date": f"{ts.astimezone(tz):%Y-%m-%d}", "quote": quote}
        if quote and len(p.evidence) < MAX_EVIDENCE and entry not in p.evidence:
            p.evidence.append(entry)

    for rec in episodes:
        slug = category_of.get(rec.id)
        if slug is None:
            continue
        quote = rec.ep.quotes[0] if rec.ep.quotes else rec.ep.summary
        marked_chosen: set[int] = set()
        for o in rec.ep.options_considered:
            p = book.get(slug, o.name)
            if p is None:
                continue
            note(p, STANCE_SCORE.get(o.stance, 0.0), [o.by], rec.ts, o.reason or quote)
            p.confidence = max(p.confidence, rec.ep.confidence)
            if o.stance == "chosen":
                marked_chosen.add(id(p))
        if rec.chosen:  # the choice counts for everyone it was for, even if proposed above
            p = book.get(slug, rec.ep.choice)
            if p is not None and id(p) not in marked_chosen:
                who = users if rec.ep.for_users == "both" else [rec.ep.for_users]
                note(p, STANCE_SCORE["chosen"], who, rec.ts, quote)
                p.confidence = max(p.confidence, rec.ep.confidence)

    for opt, ts in seen:
        mapped = alias_to_slug.get(normalise(opt.category_phrase))
        if mapped is None:
            continue
        p = book.get(mapped, opt.name)
        if p is None:
            continue
        note(p, _clamp(opt.sentiment, -1.0, 1.0), [], ts, "")
        p.tags = _unique([*p.tags, *(t.casefold() for t in opt.tags)])[:8]
        p.confidence = max(p.confidence, 0.6)

    return [p for props in book.by_cat.values() for p in props if p.scores]


def canonical_choice(options: Sequence[OptionProposal], slug: str, choice: str) -> str:
    for p in options:
        if p.category_slug == slug and _same(p.name, choice):
            return p.name
    return choice.strip()


# --- facts and notes (§15.3 step 3) -----------------------------------------------------------


@dataclass
class FactRec:
    id: str  # "f12"
    fact: Fact
    ts: datetime


def filter_facts(facts: Sequence[FactRec], owners: Sequence[str]) -> list[FactRec]:
    """Owners must be a user or "shared" (third parties are never stored, §15.4). Facts under
    0.6 confidence survive only when another fact says much the same."""
    valid = [f for f in facts if f.fact.owner in owners and f.fact.statement.strip()]
    out = []
    for f in valid:
        if f.fact.confidence >= MIN_FACT_CONFIDENCE or any(
            g is not f
            and g.fact.owner == f.fact.owner
            and fuzz.token_set_ratio(g.fact.statement, f.fact.statement) >= SAME_FACT
            for g in valid
        ):
            out.append(f)
    return out


def fact_line(rec: FactRec, tz: ZoneInfo) -> str:
    f = rec.fact
    return (
        f'{rec.id} | {rec.ts.astimezone(tz):%Y-%m-%d} | {f.type} | {f.statement} | "{f.quote}" '
        f"| {f.confidence:.2f}"
    )


@dataclass
class NoteProposal:
    owner: str
    path: str
    title: str
    lines: list[dict[str, Any]]  # {text, confidence, evidence: [{date, quote}]}

    @property
    def confidence(self) -> float:
        return round(fmean(ln["confidence"] for ln in self.lines), 2) if self.lines else 0.0

    def payload(self) -> dict[str, Any]:
        return {"owner": self.owner, "path": self.path, "title": self.title, "lines": self.lines}


def note_path(owner: str, target: str, topic: str) -> tuple[str, str]:
    """(vault path, title) for a merged note. Profiles hold hard constraints and are pinned."""
    if target == "profile":
        if owner == "shared":
            return "shared/household.md", "Household"
        return f"people/{owner}.md", owner.capitalize()
    slug = nt.slug_for_title(topic or "notes")
    title = (topic or "notes").strip().capitalize()
    rel = f"shared/topics/{slug}.md" if owner == "shared" else f"memories/{owner}/{slug}.md"
    return nt.normalise_rel(rel), title


def validate_notes(
    raw: Mapping[str, Any], owner: str, facts: Mapping[str, FactRec], tz: ZoneInfo
) -> list[NoteProposal]:
    by_path: dict[str, NoteProposal] = {}
    for item in raw.get("notes") or []:
        path, title = note_path(owner, str(item.get("target", "topic")), str(item.get("topic", "")))
        note = by_path.setdefault(path, NoteProposal(owner, path, title, []))
        for line in item.get("lines") or []:
            text = " ".join(str(line.get("text", "")).split())[:300]
            if not text:
                continue
            refs = [facts[i] for i in line.get("fact_ids") or [] if i in facts]
            note.lines.append(
                {
                    "text": text,
                    "confidence": round(_clamp(float(line.get("confidence") or 0), 0.0, 1.0), 2),
                    "evidence": [
                        {"date": f"{r.ts.astimezone(tz):%Y-%m-%d}", "quote": r.fact.quote}
                        for r in refs[:MAX_EVIDENCE]
                        if r.fact.quote
                    ],
                }
            )
    return [n for n in by_path.values() if n.lines]
