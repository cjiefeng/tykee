"""Numbered SQL migrations (``NNNN_name.sql``), each applied atomically and recorded in
``schema_version``. Run at startup, or manually with ``python -m app.db.migrate``."""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    path: Path


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    found: list[Migration] = []
    for p in sorted(directory.iterdir()):
        m = _NAME.match(p.name)
        if m:
            found.append(Migration(int(m.group(1)), p))
    versions = [m.version for m in found]
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError(f"migrations must be numbered 0001.. without gaps, got {versions}")
    return found


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if row is None:
        return 0
    (v,) = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()
    return int(v)


def apply_migrations(conn: sqlite3.Connection, directory: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply pending migrations; returns the versions applied. ``conn`` must be in autocommit
    mode (``isolation_level=None``) so we control the transaction boundaries."""
    applied: list[int] = []
    for mig in discover(directory):
        if mig.version <= current_version(conn):
            continue
        sql = mig.path.read_text(encoding="utf-8")
        # executescript() runs statements verbatim, so BEGIN/COMMIT here make it one transaction.
        record = f"INSERT INTO schema_version(version) VALUES ({mig.version});"
        script = f"BEGIN;\n{sql}\n{record}\nCOMMIT;"
        try:
            conn.executescript(script)
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            log.error("migration failed", extra={"version": mig.version})
            raise
        log.info("migration applied", extra={"version": mig.version})
        applied.append(mig.version)
    return applied


def main() -> None:
    from app.config import Env
    from app.db.database import open_writer
    from app.logging import setup_logging

    env = Env()
    setup_logging(env.log_level)
    env.data_dir.mkdir(parents=True, exist_ok=True)
    conn = open_writer(env.db_path)
    try:
        applied = apply_migrations(conn)
        log.info("migrations done", extra={"applied": applied, "version": current_version(conn)})
    finally:
        conn.close()


if __name__ == "__main__":
    main()
