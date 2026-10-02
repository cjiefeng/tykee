"""Decision engine (§8.2-8.3): candidate set → weights → sampling without replacement.

Randomness lives here, never in the LLM. The pure functions (``recency_factor``, ``sample``)
are separated from the DB-backed ``pick`` so the math is testable on its own.
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from app.decisions.categories import Category
from app.timeutil import from_sql, to_sql

MAX_N = 5
_SESSION_STATUSES = ("rerolled", "rejected")
_rng: random.Random = random.SystemRandom()


@dataclass(frozen=True)
class ExtraCandidate:
    name: str
    tags: list[str]


@dataclass(frozen=True)
class PickRequest:
    """Everything needed to (re-)run a pick; stored as ``decisions.context_json``."""

    category_id: int
    for_users: str  # a user slug or 'both'
    n: int | None = None
    include_tags: list[str] = field(default_factory=list)
    exclude_tags: list[str] = field(default_factory=list)
    extra_candidates: list[ExtraCandidate] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> PickRequest:
        d = json.loads(raw)
        d["extra_candidates"] = [ExtraCandidate(**c) for c in d.get("extra_candidates", [])]
        return cls(**d)


@dataclass(frozen=True)
class Candidate:
    name: str
    tags: tuple[str, ...]
    base_weight: float = 1.0
    option_id: int | None = None

    @property
    def key(self) -> str:
        return f"o:{self.option_id}" if self.option_id is not None else f"g:{self.name.casefold()}"


@dataclass(frozen=True)
class Pick:
    decision_id: int
    name: str
    option_id: int | None
    tags: tuple[str, ...]


@dataclass(frozen=True)
class PickResult:
    category: Category
    picks: list[Pick]
    considered: int
    weights: dict[str, float]  # candidate key → final weight (for tests and debugging)
    hard_excluded: tuple[str, ...] = ()  # avoid_tags applied from people/<slug>.md (§8.2)


# --- pure math -------------------------------------------------------------------------------


def recency_factor(days_since: float | None, tau_days: float) -> float:
    """``1 - exp(-Δt/τ)``; never picked → 1.0 (§8.3)."""
    if days_since is None:
        return 1.0
    return 1.0 - math.exp(-max(days_since, 0.0) / tau_days)


def sample(weights: Sequence[float], n: int, rng: random.Random) -> list[int]:
    """Weighted sampling of ``n`` distinct indices. Uniform over the rest if all weights are 0."""
    remaining = list(range(len(weights)))
    chosen: list[int] = []
    while remaining and len(chosen) < n:
        w = [weights[i] for i in remaining]
        if sum(w) <= 0:
            w = [1.0] * len(remaining)
        idx = rng.choices(remaining, weights=w, k=1)[0]
        chosen.append(idx)
        remaining.remove(idx)
    return chosen


def tags_ok(tags: Sequence[str], include: Sequence[str], exclude: Sequence[str]) -> bool:
    have = {t.casefold() for t in tags}
    return {t.casefold() for t in include} <= have and not have & {t.casefold() for t in exclude}


# --- DB-backed pick --------------------------------------------------------------------------


def owner_scope(for_users: str, all_slugs: Sequence[str]) -> list[str]:
    return [*all_slugs, "shared"] if for_users == "both" else [for_users, "shared"]


def _candidates(
    conn: sqlite3.Connection,
    category: Category,
    req: PickRequest,
    owners: Sequence[str],
    hard_exclude: Collection[str] = (),
) -> list[Candidate]:
    exclude = [*req.exclude_tags, *hard_exclude]
    rows = conn.execute(
        f"SELECT id, name, tags_json, base_weight, owner, active FROM options "
        f"WHERE category_id = ? AND owner IN ({','.join('?' * len(owners))})",
        (category.id, *owners),
    ).fetchall()
    out = [
        Candidate(r["name"], tuple(json.loads(r["tags_json"])), r["base_weight"], r["id"])
        for r in rows
        if r["active"]
    ]
    out = [c for c in out if tags_ok(c.tags, req.include_tags, exclude)]
    if category.allow_generated and req.extra_candidates:
        known = {
            r["name"].casefold()
            for r in conn.execute("SELECT name FROM options WHERE category_id = ?", (category.id,))
        }
        for x in req.extra_candidates:
            name = x.name.strip()
            if name and name.casefold() not in known and tags_ok(x.tags, req.include_tags, exclude):
                known.add(name.casefold())
                out.append(Candidate(name, tuple(x.tags)))
    return out


def _session_excluded(
    conn: sqlite3.Connection, category_id: int, chat_id: int | None, since: str
) -> set[str]:
    rows = conn.execute(
        "SELECT option_id, choice_text FROM decisions WHERE category_id = ? AND chat_id IS ? "
        "AND status IN (?, ?) AND created_at >= ?",
        (category_id, chat_id, *_SESSION_STATUSES, since),
    ).fetchall()
    keys: set[str] = set()
    for r in rows:
        keys.add(f"g:{r['choice_text'].casefold()}")
        if r["option_id"] is not None:
            keys.add(f"o:{r['option_id']}")
    return keys


def _last_accepted(conn: sqlite3.Connection, category_id: int) -> dict[str, str]:
    """Most recent accepted time per option id and per casefolded choice text."""
    last: dict[str, str] = {}
    for r in conn.execute(
        "SELECT option_id, choice_text, MAX(created_at) AS t FROM decisions "
        "WHERE category_id = ? AND status = 'accepted' GROUP BY option_id, lower(choice_text)",
        (category_id,),
    ):
        for key in (
            f"g:{r['choice_text'].casefold()}",
            f"o:{r['option_id']}" if r["option_id"] is not None else None,
        ):
            if key and (key not in last or r["t"] > last[key]):
                last[key] = r["t"]
    return last


def option_prefs(
    conn: sqlite3.Connection, option_ids: Sequence[int], user_ids: Sequence[int]
) -> dict[int, float]:
    if not option_ids or not user_ids:
        return {}
    rows = conn.execute(
        f"SELECT option_id, multiplier FROM option_prefs "
        f"WHERE option_id IN ({','.join('?' * len(option_ids))}) "
        f"AND user_id IN ({','.join('?' * len(user_ids))})",
        (*option_ids, *user_ids),
    ).fetchall()
    out: dict[int, float] = {}
    for r in rows:
        out[r["option_id"]] = out.get(r["option_id"], 1.0) * r["multiplier"]
    return out


def pick(
    conn: sqlite3.Connection,
    category: Category,
    req: PickRequest,
    *,
    users_by_slug: Mapping[str, int],
    asked_by: int,
    chat_id: int | None,
    now: datetime,
    session_hours: float,
    rng: random.Random | None = None,
    hard_exclude: Collection[str] = (),
) -> PickResult:
    """Weighted pick; inserts one ``decisions`` row (status 'suggested') per pick."""
    owners = owner_scope(req.for_users, list(users_by_slug))
    pref_users = (
        list(users_by_slug.values())
        if req.for_users == "both"
        else [users_by_slug[req.for_users]]
        if req.for_users in users_by_slug
        else []
    )
    cands = _candidates(conn, category, req, owners, hard_exclude)
    excluded = _session_excluded(
        conn, category.id, chat_id, to_sql(now - timedelta(hours=session_hours))
    )
    cands = [c for c in cands if c.key not in excluded and f"g:{c.name.casefold()}" not in excluded]

    last = _last_accepted(conn, category.id)
    prefs = option_prefs(conn, [c.option_id for c in cands if c.option_id is not None], pref_users)
    weights: list[float] = []
    for c in cands:
        t = last.get(c.key) or last.get(f"g:{c.name.casefold()}")
        days = (now - from_sql(t)).total_seconds() / 86400 if t else None
        pref = prefs.get(c.option_id, 1.0) if c.option_id is not None else 1.0
        weights.append(c.base_weight * pref * recency_factor(days, category.recency_tau_days))

    n = min(max(req.n or category.default_n, 1), MAX_N)
    chosen = sample(weights, n, rng or _rng)
    picks: list[Pick] = []
    context = req.to_json()
    for i in chosen:
        c = cands[i]
        cur = conn.execute(
            "INSERT INTO decisions(category_id, option_id, choice_text, for_users, asked_by, "
            "status, chat_id, context_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'suggested', ?, ?, ?)",
            (
                category.id,
                c.option_id,
                c.name,
                req.for_users,
                asked_by,
                chat_id,
                context,
                to_sql(now),
            ),
        )
        assert cur.lastrowid is not None
        picks.append(Pick(cur.lastrowid, c.name, c.option_id, c.tags))
    return PickResult(
        category=category,
        picks=picks,
        considered=len(cands),
        weights={c.key: w for c, w in zip(cands, weights, strict=True)},
        hard_excluded=tuple(sorted(hard_exclude)),
    )
