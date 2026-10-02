from __future__ import annotations

from datetime import UTC, datetime

from app.db.repos.messages import StoredMessage
from app.orchestrator.history import build_messages
from app.orchestrator.prompt import build_system, dynamic_context
from tests.conftest import Env


def _row(i: int, role: str, text: str, user_id: int | None = None) -> StoredMessage:
    return StoredMessage(i, -1, i, user_id, role, "text", text, "2026-10-01 10:00:00")  # type: ignore[arg-type]


def test_group_turns_collapse_with_speaker_tags(env: Env) -> None:
    users = {u.id: u for u in env.users}
    rows = [
        _row(1, "assistant", "orphan bot line"),
        _row(2, "user", "what to eat", env.jack.id),
        _row(3, "user", "anything lah", env.partner.id),
        _row(4, "assistant", "Ramen."),
        _row(5, "user", "  ", env.jack.id),
        _row(6, "user", "ok but not spicy", env.jack.id),
    ]
    assert build_messages(rows, users, is_group=True) == [
        {"role": "user", "content": "[Jack] what to eat\n[Partner] anything lah"},
        {"role": "assistant", "content": "Ramen."},
        {"role": "user", "content": "[Jack] ok but not spicy"},
    ]


def test_dm_turns_have_no_speaker_tags(env: Env) -> None:
    users = {u.id: u for u in env.users}
    rows = [_row(1, "user", "hi", env.jack.id), _row(2, "tool", "{}")]
    assert build_messages(rows, users, is_group=False) == [{"role": "user", "content": "hi"}]


def test_system_blocks_cache_static_prefix_only(env: Env) -> None:
    dyn = dynamic_context(
        now=datetime(2026, 10, 2, 11, 0, tzinfo=UTC), actor=env.jack, users=env.users,
        is_group=True, default_for_users="both",
    )  # fmt: skip
    assert "19:00 (Asia/Singapore)" in dyn and "for both" in dyn
    assert dyn.endswith("Default for_users: both")
    blocks = build_system("PERSONA", dyn)
    assert [b.get("cache_control") is not None for b in blocks] == [False, True, False]
    assert blocks[0]["text"] == "PERSONA" and blocks[-1]["text"] == dyn
    with_pinned = build_system("PERSONA", dyn, pinned="allergic to peanuts")
    assert [b.get("cache_control") is not None for b in with_pinned] == [False, True, True, False]
