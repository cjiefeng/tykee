"""Hybrid retrieval (§6.5): FTS5 BM25 + vector KNN, fused with Reciprocal Rank Fusion, then
1-hop wikilink neighbours. Owner filtering happens after over-fetching (30 per side), which is
simpler than vec0 partitions and free at this data size."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from app.brain.embedder import Embedder
from app.brain.index import pack
from app.db.database import Database

OVERFETCH = 30
RRF_K = 60
MAX_NEIGHBOURS = 3
SNIPPET_CHARS = 400
_WORD = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True)
class Hit:
    path: str
    title: str
    heading: str | None
    snippet: str
    score: float
    owner: str
    via: str  # 'match' | 'link'


def fts_query(text: str) -> str | None:
    """User text → a safe FTS5 query: each word quoted, OR-ed (no operator injection)."""
    words = [w for w in _WORD.findall(text.casefold()) if len(w) > 1 or not w.isascii()]
    if not words:
        return None
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(words))


def _fts_ids(conn: sqlite3.Connection, query: str) -> list[int]:
    q = fts_query(query)
    if q is None:
        return []
    rows = conn.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?",
        (q, OVERFETCH),
    ).fetchall()
    return [int(r[0]) for r in rows]


def _vec_ids(conn: sqlite3.Connection, vector: bytes) -> list[int]:
    rows = conn.execute(
        "SELECT rowid FROM chunks_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (vector, OVERFETCH),
    ).fetchall()
    return [int(r[0]) for r in rows]


@dataclass(frozen=True)
class _ChunkInfo:
    id: int
    note_id: int
    path: str
    owner: str
    title: str
    heading: str | None
    text: str


def _chunk_info(conn: sqlite3.Connection, ids: Collection[int]) -> dict[int, _ChunkInfo]:
    if not ids:
        return {}
    rows = conn.execute(
        f"SELECT c.id, c.note_id, c.heading, c.text, n.path, n.owner, n.title FROM chunks c "
        f"JOIN notes n ON n.id = c.note_id WHERE c.id IN ({','.join('?' * len(ids))})",
        tuple(ids),
    ).fetchall()
    return {
        r["id"]: _ChunkInfo(
            r["id"], r["note_id"], r["path"], r["owner"], r["title"], r["heading"], r["text"]
        )
        for r in rows
    }


def _snippet(text: str) -> str:
    body = text.split("\n", 1)[1] if "\n" in text else text  # drop the 'title > heading' prefix
    body = " ".join(body.split())
    return body if len(body) <= SNIPPET_CHARS else body[: SNIPPET_CHARS - 1] + "…"


def rrf(rankings: Sequence[Sequence[int]], k: int = RRF_K) -> dict[int, float]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return scores


def search_sync(
    conn: sqlite3.Connection,
    query: str,
    query_vector: bytes | None,
    owners: Collection[str],
    k: int,
) -> list[Hit]:
    fts = _fts_ids(conn, query)
    vec = _vec_ids(conn, query_vector) if query_vector is not None else []
    info = _chunk_info(conn, set(fts) | set(vec))
    allowed = set(owners)
    fts = [i for i in fts if i in info and info[i].owner in allowed]
    vec = [i for i in vec if i in info and info[i].owner in allowed]
    fused = sorted(rrf([fts, vec]).items(), key=lambda kv: (-kv[1], kv[0]))[:k]

    hits = [
        Hit(
            info[cid].path,
            info[cid].title,
            info[cid].heading,
            _snippet(info[cid].text),
            score,
            info[cid].owner,
            "match",
        )
        for cid, score in fused
    ]
    if not fused:
        return hits

    # 1-hop neighbours via [[links]], owner-filtered, scored below every direct match.
    seen_paths = {h.path for h in hits}
    note_ids = sorted({info[cid].note_id for cid, _ in fused})
    floor = min(score for _, score in fused) / 2
    rows = conn.execute(
        f"SELECT DISTINCT n.id, n.path, n.owner, n.title FROM links l "
        f"JOIN notes n ON n.path = l.dst_path "
        f"WHERE l.src_note_id IN ({','.join('?' * len(note_ids))}) ORDER BY n.path",
        note_ids,
    ).fetchall()
    added = 0
    for r in rows:
        if added >= MAX_NEIGHBOURS or r["path"] in seen_paths or r["owner"] not in allowed:
            continue
        first = conn.execute(
            "SELECT heading, text FROM chunks WHERE note_id = ? ORDER BY ord LIMIT 1", (r["id"],)
        ).fetchone()
        if first is None:
            continue
        snippet = _snippet(first["text"])
        hits.append(
            Hit(r["path"], r["title"], first["heading"], snippet, floor, r["owner"], "link")
        )
        seen_paths.add(r["path"])
        added += 1
    return hits


class Retriever:
    def __init__(self, db: Database, embedder: Embedder) -> None:
        self._db = db
        self._embedder = embedder

    async def search(self, query: str, owners: Collection[str], k: int = 6) -> list[Hit]:
        try:
            vector: bytes | None = pack(await self._embedder.embed_query(query))
        except Exception:  # embedding failure degrades to keyword-only search
            vector = None
        return await self._db.read(lambda c: search_sync(c, query, vector, owners, k))
