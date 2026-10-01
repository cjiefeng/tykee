from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.db.database import open_writer
from app.db.migrate import apply_migrations, current_version, discover

EXPECTED_TABLES = {
    "users", "settings", "categories", "category_aliases", "options", "option_prefs",
    "decisions", "messages", "chat_summaries", "notes", "chunks", "links", "chunks_fts",
    "chunks_vec", "memory_inbox", "import_jobs", "import_windows", "import_items", "usage",
    "chat_state", "ambient_log", "schema_version",
}  # fmt: skip


def test_fresh_db_migrates_to_latest(tmp_path: Path) -> None:
    conn = open_writer(tmp_path / "t.db")
    applied = apply_migrations(conn)
    assert applied == [m.version for m in discover()]
    assert current_version(conn) == applied[-1]
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables >= EXPECTED_TABLES
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    conn = open_writer(tmp_path / "t.db")
    apply_migrations(conn)
    assert apply_migrations(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == len(discover())


def test_vec_and_fts_tables_work(tmp_path: Path) -> None:
    conn = open_writer(tmp_path / "t.db")
    apply_migrations(conn)
    import struct

    conn.execute(
        "INSERT INTO chunks_vec(rowid, embedding) VALUES (1, ?)",
        (struct.pack("384f", *([0.1] * 384)),),
    )
    (n,) = conn.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()
    assert n == 1
    conn.execute("INSERT INTO chunks_fts(rowid, text) VALUES (1, 'spicy noodles')")
    assert conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'spicy'").fetchall()


def test_failed_migration_rolls_back(tmp_path: Path) -> None:
    mig_dir = tmp_path / "migs"
    mig_dir.mkdir()
    (mig_dir / "0001_ok.sql").write_text(
        "CREATE TABLE a (x INTEGER); CREATE TABLE schema_version (version INTEGER NOT NULL);"
    )
    (mig_dir / "0002_bad.sql").write_text("CREATE TABLE b (y INTEGER); THIS IS NOT SQL;")
    conn = open_writer(tmp_path / "t.db")
    with pytest.raises(sqlite3.Error):
        apply_migrations(conn, mig_dir)
    assert current_version(conn) == 1
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "a" in tables and "b" not in tables
    assert not conn.in_transaction


def test_gap_in_numbering_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("SELECT 1;")
    (tmp_path / "0003_c.sql").write_text("SELECT 1;")
    with pytest.raises(RuntimeError):
        discover(tmp_path)
