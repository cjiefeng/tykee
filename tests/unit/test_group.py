from __future__ import annotations

from app.telegram.group import GroupRegistry, resolve_group_id
from tests.conftest import Env


async def test_env_seeds_group_id(env: Env) -> None:
    assert await env.db.write(lambda c: resolve_group_id(c, -1)) == -1
    assert await env.db.write(lambda c: resolve_group_id(c, None)) == -1


async def test_stale_env_after_migration_maps_to_new_id(env: Env) -> None:
    await env.db.write(lambda c: resolve_group_id(c, -1))
    reg = GroupRegistry(env.db, -1)
    assert await reg.migrate(-1, -1001)
    assert reg.is_allowed(-1001) and not reg.is_allowed(-1)
    assert await env.db.write(lambda c: resolve_group_id(c, -1)) == -1001


async def test_new_env_value_wins_over_stored(env: Env) -> None:
    await env.db.write(lambda c: resolve_group_id(c, -1))
    assert await env.db.write(lambda c: resolve_group_id(c, -2)) == -2


async def test_migration_of_other_group_is_ignored(env: Env) -> None:
    reg = GroupRegistry(env.db, -1)
    assert not await reg.migrate(-5, -6)
    assert reg.group_id == -1
