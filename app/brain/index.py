"""Derived search index over the vault (§5.2, §6.3): notes, chunks, chunks_fts, chunks_vec,
links. Everything here is rebuildable from the markdown files.

FTS5 uses external content (``content='chunks'``), so deletes must pass the old text, and
virtual tables don't take part in ``ON DELETE CASCADE``; both are handled explicitly here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from app.brain.notes import Chunk

PENDING = "pending"  # embed_model for chunks whose embedding failed (§14.4); retried on reconcile


def pack(vec: Sequence[float]) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


@dataclass(frozen=True)
class IndexedChunk:
    chunk: Chunk
    vector: bytes | None  # packed float32[384]; None → embed_model 'pending'
    embed_model: str


@dataclass(frozen=True)
class NoteRow:
    path: str
    owner: str
    type: str
    title: str
    file_hash: str
    pinned: bool
    updated_at: str


def note_id(conn: sqlite3.Connection, path: str) -> int | None:
    r = conn.execute("SELECT id FROM notes WHERE path = ?", (path,)).fetchone()
    return int(r["id"]) if r else None


def file_hashes(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["path"]: r["file_hash"] for r in conn.execute("SELECT path, file_hash FROM notes")}


def reusable_vectors(conn: sqlite3.Connection, path: str, model: str) -> dict[str, bytes]:
    """chunk_hash → stored vector for this note's chunks embedded with ``model``."""
    rows = conn.execute(
        "SELECT c.chunk_hash, v.embedding FROM chunks c JOIN notes n ON n.id = c.note_id "
        "JOIN chunks_vec v ON v.rowid = c.id WHERE n.path = ? AND c.embed_model = ?",
        (path, model),
    ).fetchall()
    return {r["chunk_hash"]: bytes(r["embedding"]) for r in rows}


def _delete_chunks(conn: sqlite3.Connection, nid: int) -> None:
    for r in conn.execute("SELECT id, text FROM chunks WHERE note_id = ?", (nid,)).fetchall():
        conn.execute(
            "INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', ?, ?)",
            (r["id"], r["text"]),
        )
        conn.execute("DELETE FROM chunks_vec WHERE rowid = ?", (r["id"],))
    conn.execute("DELETE FROM chunks WHERE note_id = ?", (nid,))
    conn.execute("DELETE FROM links WHERE src_note_id = ?", (nid,))


def replace_note(
    conn: sqlite3.Connection,
    row: NoteRow,
    chunks: Sequence[IndexedChunk],
    links: Sequence[str],
) -> int:
    """Upsert the note and replace all of its derived rows (one transaction via db.write)."""
    conn.execute(
        "INSERT INTO notes(path, owner, type, title, file_hash, pinned, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET owner = excluded.owner, "
        "type = excluded.type, title = excluded.title, file_hash = excluded.file_hash, "
        "pinned = excluded.pinned, updated_at = excluded.updated_at",
        (row.path, row.owner, row.type, row.title, row.file_hash, int(row.pinned), row.updated_at),
    )
    nid = note_id(conn, row.path)
    assert nid is not None
    _delete_chunks(conn, nid)
    for ic in chunks:
        cur = conn.execute(
            "INSERT INTO chunks(note_id, ord, heading, text, chunk_hash, embed_model) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (nid, ic.chunk.ord, ic.chunk.heading, ic.chunk.text, ic.chunk.hash, ic.embed_model),
        )
        cid = cur.lastrowid
        conn.execute("INSERT INTO chunks_fts(rowid, text) VALUES (?, ?)", (cid, ic.chunk.text))
        if ic.vector is not None:
            conn.execute("INSERT INTO chunks_vec(rowid, embedding) VALUES (?, ?)", (cid, ic.vector))
    for dst in links:
        conn.execute("INSERT OR IGNORE INTO links(src_note_id, dst_path) VALUES (?, ?)", (nid, dst))
    return nid


def delete_note(conn: sqlite3.Connection, path: str) -> bool:
    nid = note_id(conn, path)
    if nid is None:
        return False
    _delete_chunks(conn, nid)
    conn.execute("DELETE FROM notes WHERE id = ?", (nid,))
    return True


def stale_paths(conn: sqlite3.Connection, model: str) -> list[str]:
    """Notes with any chunk not embedded by ``model`` (pending, or a different model/precision)."""
    return [
        r["path"]
        for r in conn.execute(
            "SELECT DISTINCT n.path FROM notes n JOIN chunks c ON c.note_id = n.id "
            "WHERE c.embed_model != ? ORDER BY n.path",
            (model,),
        )
    ]


def drop_all(conn: sqlite3.Connection) -> None:
    """System → Reindex (§14.4): wipe the derived index; files are untouched."""
    conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('delete-all')")
    conn.execute("DELETE FROM chunks_vec")
    conn.execute("DELETE FROM links")
    conn.execute("DELETE FROM chunks")
    conn.execute("DELETE FROM notes")


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        t: int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        for t in ("notes", "chunks", "chunks_vec", "links")
    }
