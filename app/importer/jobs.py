"""import_jobs / import_windows / import_items queries and the review-step edits (§15.2 ⑦).
Functions take a sqlite3 connection and run via ``db.read`` / ``db.write``."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.decisions.categories import TAU_MAX, TAU_MIN
from app.decisions.text import ALIAS_MAX, normalise, slugify

ACTIVE = ("extracting", "consolidating", "applying")
EDITABLE = ("uploaded", "configured")
REOPENABLE = ("cancelled", "failed")
KINDS = ("category", "option", "note", "decision")


class ImportProblem(ValueError):
    """A request the import can't carry out; the message is shown in the dashboard."""


@dataclass(frozen=True)
class Job:
    id: int
    status: str
    filename: str
    format: str
    file_sha256: str
    since: str
    until: str
    msg_count: int
    window_count: int
    cost_usd: float
    est_cost_usd: float | None
    consent_at: str | None
    created_at: str
    error: str | None
    attempts: int
    polled_at: str | None
    skipped_out_of_scope: int
    meta: dict[str, Any]
    chats: list[str]
    sender_map: dict[str, str]
    consolidation: dict[str, Any]
    summary: dict[str, Any]

    @property
    def chat_names(self) -> dict[str, str]:
        return {c["ref"]: c["name"] for c in self.meta.get("chats", [])}


def _j(value: str | None, default: Any) -> Any:
    return json.loads(value) if value else default


def _job(r: sqlite3.Row) -> Job:
    return Job(
        id=r["id"],
        status=r["status"],
        filename=r["filename"] or "",
        format=r["format"] or "single",
        file_sha256=r["file_sha256"],
        since=r["since"],
        until=r["until"] or "",
        msg_count=r["msg_count"] or 0,
        window_count=r["window_count"] or 0,
        cost_usd=r["cost_usd"],
        est_cost_usd=r["est_cost_usd"],
        consent_at=r["consent_at"],
        created_at=r["created_at"],
        error=r["error"],
        attempts=r["attempts"],
        polled_at=r["polled_at"],
        skipped_out_of_scope=r["skipped_out_of_scope"],
        meta=_j(r["meta_json"], {}),
        chats=_j(r["chats_json"], []),
        sender_map=_j(r["sender_map_json"], {}),
        consolidation=_j(r["consolidation_json"], {}),
        summary=_j(r["summary_json"], {}),
    )


def get(conn: sqlite3.Connection, job_id: int) -> Job | None:
    r = conn.execute("SELECT * FROM import_jobs WHERE id = ?", (job_id,)).fetchone()
    return _job(r) if r else None


def by_sha(conn: sqlite3.Connection, sha: str) -> Job | None:
    r = conn.execute("SELECT * FROM import_jobs WHERE file_sha256 = ?", (sha,)).fetchone()
    return _job(r) if r else None


def all_jobs(conn: sqlite3.Connection) -> list[Job]:
    return [_job(r) for r in conn.execute("SELECT * FROM import_jobs ORDER BY id DESC")]


def with_status(conn: sqlite3.Connection, *statuses: str) -> list[Job]:
    rows = conn.execute(
        f"SELECT * FROM import_jobs WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY id",
        statuses,
    )
    return [_job(r) for r in rows]


def update(conn: sqlite3.Connection, job_id: int, **fields: Any) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE import_jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))


def set_status(
    conn: sqlite3.Connection, job_id: int, status: str, *, expect: tuple[str, ...]
) -> None:
    cur = conn.execute(
        f"UPDATE import_jobs SET status = ? WHERE id = ? "
        f"AND status IN ({','.join('?' * len(expect))})",
        (status, job_id, *expect),
    )
    if cur.rowcount == 0:
        raise ImportProblem("the import changed in the meantime; reload the page")


def spent(conn: sqlite3.Connection, job_id: int) -> float:
    (total,) = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM usage WHERE import_job_id = ?", (job_id,)
    ).fetchone()
    return float(total)


def window_counts(conn: sqlite3.Connection, job_id: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM import_windows WHERE job_id = ? GROUP BY status", (job_id,)
    )
    return {r[0]: r[1] for r in rows}


def covered_ranges(conn: sqlite3.Connection, job_id: int) -> dict[str, list[tuple[int, int]]]:
    """Message-id ranges other live (not cancelled/failed) jobs already windowed."""
    rows = conn.execute(
        "SELECT w.chat_ref, w.first_msg_id, w.last_msg_id FROM import_windows w "
        "JOIN import_jobs j ON j.id = w.job_id "
        "WHERE w.job_id != ? AND j.status NOT IN ('cancelled', 'failed', 'uploaded', "
        "'configured') AND w.first_msg_id IS NOT NULL",
        (job_id,),
    )
    out: dict[str, list[tuple[int, int]]] = {}
    for r in rows:
        out.setdefault(r[0], []).append((r[1], r[2]))
    return out


# --- items -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    id: int
    kind: str
    status: str
    payload: dict[str, Any]
    evidence: list[dict[str, Any]]
    confidence: float
    category_item_id: int | None
    ref: str | None


def _item(r: sqlite3.Row) -> Item:
    return Item(
        r["id"],
        r["kind"],
        r["status"],
        json.loads(r["payload_json"]),
        json.loads(r["evidence_json"]),
        r["confidence"],
        r["category_item_id"],
        r["ref"],
    )


def items(conn: sqlite3.Connection, job_id: int, kind: str | None = None) -> list[Item]:
    if kind is None:
        rows = conn.execute("SELECT * FROM import_items WHERE job_id = ? ORDER BY id", (job_id,))
    else:
        rows = conn.execute(
            "SELECT * FROM import_items WHERE job_id = ? AND kind = ? ORDER BY id", (job_id, kind)
        )
    return [_item(r) for r in rows]


def item(conn: sqlite3.Connection, job_id: int, item_id: int) -> Item:
    r = conn.execute(
        "SELECT * FROM import_items WHERE id = ? AND job_id = ?", (item_id, job_id)
    ).fetchone()
    if r is None:
        raise ImportProblem("no such item")
    return _item(r)


def insert_item(
    conn: sqlite3.Connection,
    job_id: int,
    kind: str,
    payload: dict[str, Any],
    evidence: list[dict[str, Any]],
    confidence: float,
    *,
    category_item_id: int | None = None,
    ref: str | None = None,
    status: str = "pending",
) -> int:
    cur = conn.execute(
        "INSERT INTO import_items(job_id, kind, payload_json, evidence_json, confidence, status, "
        "category_item_id, ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job_id,
            kind,
            json.dumps(payload, ensure_ascii=False),
            json.dumps(evidence, ensure_ascii=False),
            round(confidence, 3),
            status,
            category_item_id,
            ref,
        ),
    )
    return int(cur.lastrowid or 0)


def counts(conn: sqlite3.Connection, job_id: int) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for r in conn.execute(
        "SELECT kind, status, COUNT(*) FROM import_items WHERE job_id = ? GROUP BY kind, status",
        (job_id,),
    ):
        out.setdefault(r[0], {})[r[1]] = r[2]
    return out


def _require_review(conn: sqlite3.Connection, job_id: int) -> None:
    job = get(conn, job_id)
    if job is None or job.status != "review":
        raise ImportProblem("this import isn't waiting for review")


def _save_payload(conn: sqlite3.Connection, item_id: int, payload: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE import_items SET payload_json = ? WHERE id = ?",
        (json.dumps(payload, ensure_ascii=False), item_id),
    )


def decide(conn: sqlite3.Connection, job_id: int, item_id: int, approve: bool) -> None:
    _require_review(conn, job_id)
    it = item(conn, job_id, item_id)
    if it.kind not in KINDS or it.status not in ("pending", "approved", "rejected"):
        raise ImportProblem("that item can't be changed")
    conn.execute(
        "UPDATE import_items SET status = ? WHERE id = ?",
        ("approved" if approve else "rejected", item_id),
    )


def bulk_approve(conn: sqlite3.Connection, job_id: int, kind: str, min_conf: float) -> int:
    _require_review(conn, job_id)
    if kind not in KINDS:
        raise ImportProblem("unknown kind")
    cur = conn.execute(
        "UPDATE import_items SET status = 'approved' WHERE job_id = ? AND kind = ? "
        "AND status = 'pending' AND confidence >= ?",
        (job_id, kind, min_conf),
    )
    return cur.rowcount


def edit_category(
    conn: sqlite3.Connection,
    job_id: int,
    item_id: int,
    *,
    slug: str,
    display_name: str,
    description: str,
    tau: float,
    default_n: int,
    aliases: list[str],
) -> None:
    _require_review(conn, job_id)
    it = item(conn, job_id, item_id)
    if it.kind != "category":
        raise ImportProblem("not a category")
    new_slug = slugify(slug) or it.payload["slug"]
    for other in items(conn, job_id, "category"):
        if other.id != item_id and other.status != "merged" and other.payload["slug"] == new_slug:
            raise ImportProblem(f"another proposal already uses the slug {new_slug!r}")
    p = dict(it.payload)
    p.update(
        slug=new_slug,
        display_name=display_name.strip() or p["display_name"],
        description=description.strip(),
        recency_tau_days=min(max(tau, TAU_MIN), TAU_MAX),
        default_n=min(max(default_n, 1), 5),
        aliases=sorted({a for a in map(normalise, aliases) if 0 < len(a) <= ALIAS_MAX}),
    )
    _save_payload(conn, item_id, p)
    # Children refer to the category by item id; keep their slug label in step for display.
    for child in conn.execute(
        "SELECT id, payload_json FROM import_items WHERE category_item_id = ?", (item_id,)
    ).fetchall():
        cp = json.loads(child[1])
        cp["category_slug"] = new_slug
        _save_payload(conn, child[0], cp)


def merge_categories(conn: sqlite3.Connection, job_id: int, src_id: int, dst_id: int) -> None:
    """Fold proposal ``src`` into ``dst``: its options/decisions move over, its slug and aliases
    become ``dst`` aliases, and ``src`` is marked merged."""
    _require_review(conn, job_id)
    if src_id == dst_id:
        raise ImportProblem("pick two different categories")
    src, dst = item(conn, job_id, src_id), item(conn, job_id, dst_id)
    if src.kind != "category" or dst.kind != "category":
        raise ImportProblem("both must be category proposals")
    if "merged" in (src.status, dst.status):
        raise ImportProblem("that category was already merged")
    p = dict(dst.payload)
    p["aliases"] = sorted(
        {*p["aliases"], *src.payload["aliases"], normalise(src.payload["slug"].replace("-", " "))}
    )
    p["episode_ids"] = [*p["episode_ids"], *src.payload["episode_ids"]]
    _save_payload(conn, dst_id, p)
    conn.execute(
        "UPDATE import_items SET category_item_id = ? WHERE category_item_id = ?",
        (dst_id, src_id),
    )
    for child in conn.execute(
        "SELECT id, payload_json FROM import_items WHERE category_item_id = ?", (dst_id,)
    ).fetchall():
        cp = json.loads(child[1])
        cp["category_slug"] = p["slug"]
        _save_payload(conn, child[0], cp)
    sp = dict(src.payload, merged_into=dst_id)
    _save_payload(conn, src_id, sp)
    conn.execute("UPDATE import_items SET status = 'merged' WHERE id = ?", (src_id,))


def edit_option(
    conn: sqlite3.Connection, job_id: int, item_id: int, *, name: str, tags: list[str],
    base_weight: float,
) -> None:  # fmt: skip
    _require_review(conn, job_id)
    it = item(conn, job_id, item_id)
    if it.kind != "option":
        raise ImportProblem("not an option")
    p = dict(it.payload)
    p.update(
        name=name.strip() or p["name"],
        tags=sorted({t.strip().casefold() for t in tags if t.strip()}),
        base_weight=round(min(max(base_weight, 0.1), 3.0), 2),
    )
    _save_payload(conn, item_id, p)


def edit_note(conn: sqlite3.Connection, job_id: int, item_id: int, text: str) -> None:
    """One line per line of text; evidence stays with lines whose text didn't change."""
    _require_review(conn, job_id)
    it = item(conn, job_id, item_id)
    if it.kind != "note":
        raise ImportProblem("not a note")
    old = {ln["text"]: ln for ln in it.payload["lines"]}
    lines = []
    for raw in text.splitlines():
        t = " ".join(raw.strip().lstrip("-").split())[:300]
        if t:
            lines.append(old.get(t, {"text": t, "confidence": 1.0, "evidence": []}))
    p = dict(it.payload, lines=lines)
    _save_payload(conn, item_id, p)


def assign_unmapped(conn: sqlite3.Connection, job_id: int, item_id: int, category_id: int) -> None:
    """Give an unmapped episode a category; a chosen one becomes a decision to review."""
    _require_review(conn, job_id)
    it, cat = item(conn, job_id, item_id), item(conn, job_id, category_id)
    if it.kind != "unmapped" or it.status != "info" or cat.kind != "category":
        raise ImportProblem("can't assign that")
    if cat.status == "merged":
        cat = item(conn, job_id, int(cat.payload["merged_into"]))
    p = dict(cat.payload)
    p["episode_ids"] = [*p["episode_ids"], it.payload["episode_id"]]
    _save_payload(conn, cat.id, p)
    e = it.payload
    if e.get("choice"):
        insert_item(
            conn,
            job_id,
            "decision",
            {
                "category_slug": p["slug"],
                "choice": e["choice"],
                "for_users": e.get("for_users", "both"),
                "ts": e["ts"],
                "summary": e.get("summary", ""),
            },
            it.evidence,
            it.confidence,
            category_item_id=cat.id,
            ref=e["episode_id"],
        )
    conn.execute("UPDATE import_items SET status = 'assigned' WHERE id = ?", (item_id,))
