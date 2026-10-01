from __future__ import annotations

import sqlite3

import pytest

from tests.conftest import Env


async def test_write_then_read_from_reader_pool(env: Env) -> None:
    await env.db.write(
        lambda c: c.execute("INSERT INTO settings(key, value_json) VALUES ('t.k', '1')")
    )
    row = await env.db.read(
        lambda c: c.execute("SELECT value_json FROM settings WHERE key='t.k'").fetchone()
    )
    assert row["value_json"] == "1"


async def test_readers_cannot_write(env: Env) -> None:
    with pytest.raises(sqlite3.OperationalError):
        await env.db.read(
            lambda c: c.execute("INSERT INTO settings(key, value_json) VALUES ('x', '1')")
        )


async def test_write_rolls_back_on_error(env: Env) -> None:
    def _boom(c: sqlite3.Connection) -> None:
        c.execute("INSERT INTO settings(key, value_json) VALUES ('rb', '1')")
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await env.db.write(_boom)
    row = await env.db.read(lambda c: c.execute("SELECT 1 FROM settings WHERE key='rb'").fetchone())
    assert row is None
