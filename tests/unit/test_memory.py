"""Memory service (§6.5-6.7): scopes, read/write policy, inbox, pinned block, decision log."""

from __future__ import annotations

import pytest

from app.brain.memory import MemoryPolicyError
from app.settings import set_value
from tests.conftest import Env, Stack, make_stack


async def _ready(env: Env) -> Stack:
    stack = make_stack(env)
    await stack.store.ensure_skeleton(env.users)
    return stack


async def test_scopes(env: Env) -> None:
    m = (await _ready(env)).memory
    assert m.owners_for("me", "jack") == ["jack", "shared"]
    assert m.owners_for("partner", "jack") == ["partner", "shared"]
    assert m.owners_for("both", "jack") == ["jack", "partner", "shared"]
    assert m.owners_for("shared", "jack") == ["shared"]


async def test_read_permissions(env: Env) -> None:
    stack = await _ready(env)
    await stack.store.write("memories/partner/gift", mode="create", content="surprise")
    m = stack.memory
    with pytest.raises(MemoryPolicyError):
        await m.read("memories/partner/gift", asker="jack", is_group=False)
    rel, _ = await m.read("memories/partner/gift", asker="jack", is_group=True)
    assert rel == "memories/partner/gift.md"
    rel, note = await m.read("people/partner", asker="jack", is_group=False)  # pinned profile
    assert note.pinned
    with pytest.raises(MemoryPolicyError):
        await m.read("memories/jack/missing", asker="jack", is_group=True)


async def test_write_policy(env: Env) -> None:
    m = (await _ready(env)).memory
    for bad in ("logs/2026/10/x", "notes/x", "memories/bob/x", "../x"):
        with pytest.raises(MemoryPolicyError):
            await m.write(bad, mode="create", content="x")
    with pytest.raises(MemoryPolicyError):
        await m.write("memories/jack/x", mode="create", content="x", add_avoid_tags=["a"])
    r = await m.write("memories/partner/food", mode="create", content="Off seafood this month.")
    assert r.created


async def test_propose_goes_to_inbox_then_approve(env: Env) -> None:
    stack = await _ready(env)
    m = stack.memory
    item = await m.propose(
        owner="partner",
        content="Off seafood this month.",
        reason="said so",
        topic="Food",
        source="telegram:-1",
    )
    assert (item.status, item.target_path) == ("pending", "memories/partner/food.md")
    assert not await stack.store.exists("memories/partner/food")
    assert await m.pending_count() == 1
    done = await m.decide(item.id, approve=True, user_id=env.jack.id)
    assert done.status == "approved"
    note = await stack.store.read("memories/partner/food")
    assert note is not None and "- Off seafood this month." in note.body and note.title == "Food"
    # deciding again is a no-op
    await m.decide(item.id, approve=False, user_id=env.jack.id)
    assert (await m.get(item.id)).status == "approved"  # type: ignore[union-attr]


async def test_reject_and_shared_target(env: Env) -> None:
    stack = await _ready(env)
    item = await stack.memory.propose(
        owner="shared", content="Rice cooker broke.", reason="r", topic="kitchen stuff", source="s"
    )
    assert item.target_path == "shared/topics/kitchen-stuff.md"
    await stack.memory.decide(item.id, approve=False, user_id=env.jack.id)
    assert not await stack.store.exists(item.target_path)
    with pytest.raises(MemoryPolicyError):
        await stack.memory.propose(owner="bob", content="x", reason="r", topic="t", source="s")


async def test_auto_approve_unless_forced_review(env: Env) -> None:
    await env.db.write(lambda c: set_value(c, "memory.auto_approve", True))
    stack = await _ready(env)
    a = await stack.memory.propose(
        owner="jack", content="Likes teh peng.", reason="r", topic="drinks", source="s"
    )
    assert a.status == "approved" and await stack.store.exists("memories/jack/drinks")
    b = await stack.memory.propose(
        owner="jack",
        content="Shop X closed.",
        reason="web",
        topic="places",
        source="web:https://x",
        force_review=True,
    )
    assert b.status == "pending"


async def test_pinned_block_includes_avoid_tags_and_truncates(env: Env) -> None:
    stack = await _ready(env)
    await stack.memory.write(
        "people/jack",
        mode="append",
        content="Hates coriander.",
        add_avoid_tags=["contains:coriander"],
    )
    block = await stack.memory.pinned_block()
    assert block is not None
    assert "### people/jack.md (owner: jack)" in block and "Hates coriander." in block
    assert "Avoid tags (enforced in code): contains:coriander" in block
    assert "### shared/household.md" in block
    await env.db.write(lambda c: set_value(c, "memory.pinned_max_chars", 50))
    short = await stack.memory.pinned_block()
    assert short is not None and short.endswith("[…truncated]") and len(short) < 80


async def test_avoid_tags_for_users(env: Env) -> None:
    stack = await _ready(env)
    await stack.memory.write(
        "people/jack", mode="append", content="x", add_avoid_tags=["contains:peanut"]
    )
    await stack.memory.write(
        "people/partner", mode="append", content="x", add_avoid_tags=["raw-fish"]
    )
    assert await stack.memory.avoid_tags("jack") == {"contains:peanut"}
    assert await stack.memory.avoid_tags("both") == {"contains:peanut", "raw-fish"}


async def test_log_decision_failure_is_swallowed(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    stack = await _ready(env)

    async def boom(line: str) -> str:
        raise OSError("disk full")

    monkeypatch.setattr(stack.store, "append_log", boom)
    await stack.memory.log_decision("- x")  # must not raise
