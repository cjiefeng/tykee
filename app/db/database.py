"""SQLite access (§5.1): one writer connection on a dedicated thread, a pool of read-only ones.

All callers pass a plain function ``fn(conn) -> T``; it runs on the right thread and the result
is awaited. Writes run inside ``BEGIN IMMEDIATE`` / ``COMMIT`` (rolled back on any exception).
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypeVar

import sqlite_vec

T = TypeVar("T")

PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
)


def _load_vec(conn: sqlite3.Connection) -> None:
    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)


def open_writer(path: Path) -> sqlite3.Connection:
    # isolation_level=None: we issue BEGIN/COMMIT ourselves.
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=True)
    conn.row_factory = sqlite3.Row
    _load_vec(conn)
    for p in PRAGMAS:
        conn.execute(p)
    return conn


def open_reader(path: Path) -> sqlite3.Connection:
    # check_same_thread=False only so close() can run after the pool shuts down; each reader is
    # otherwise used exclusively by the pool thread that opened it.
    conn = sqlite3.connect(
        f"file:{path}?mode=ro", uri=True, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    _load_vec(conn)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA query_only = ON")
    return conn


class Database:
    def __init__(self, path: Path, read_pool_size: int = 3) -> None:
        self.path = path
        self._writer_exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db-writer")
        self._reader_exec = ThreadPoolExecutor(
            max_workers=read_pool_size, thread_name_prefix="db-reader"
        )
        self._writer: sqlite3.Connection | None = None
        self._local = threading.local()
        self._readers: list[sqlite3.Connection] = []
        self._readers_lock = threading.Lock()

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        def _open() -> None:
            self._writer = open_writer(self.path)

        await asyncio.get_running_loop().run_in_executor(self._writer_exec, _open)

    async def run_raw(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run on the writer thread without an implicit transaction (migrations, VACUUM)."""

        def _run() -> T:
            assert self._writer is not None, "Database.open() not called"
            return fn(self._writer)

        return await asyncio.get_running_loop().run_in_executor(self._writer_exec, _run)

    async def write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        def _run() -> T:
            conn = self._writer
            assert conn is not None, "Database.open() not called"
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(conn)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return result

        return await asyncio.get_running_loop().run_in_executor(self._writer_exec, _run)

    async def read(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        def _run() -> T:
            conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
            if conn is None:
                conn = open_reader(self.path)
                self._local.conn = conn
                with self._readers_lock:
                    self._readers.append(conn)
            return fn(conn)

        return await asyncio.get_running_loop().run_in_executor(self._reader_exec, _run)

    async def close(self) -> None:
        def _close_writer() -> None:
            if self._writer is not None:
                self._writer.close()
                self._writer = None

        await asyncio.get_running_loop().run_in_executor(self._writer_exec, _close_writer)
        self._writer_exec.shutdown(wait=True)
        self._reader_exec.shutdown(wait=True)
        # Reader threads are gone; their connections are safe to close from here.
        with self._readers_lock:
            for conn in self._readers:
                conn.close()
            self._readers.clear()
