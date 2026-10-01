from __future__ import annotations

import pytest

from app.config import Env, parse_allowlist


def test_allowlist_first_entry_is_admin() -> None:
    users = parse_allowlist(" 111:Jack , 222:partner ")
    assert [(u.telegram_id, u.slug, u.is_admin) for u in users] == [
        (111, "jack", True),
        (222, "partner", False),
    ]


@pytest.mark.parametrize("raw", ["", "111", "111:", "111:a,111:b", "111:a,222:a", "x:a"])
def test_allowlist_rejects_bad_input(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_allowlist(raw)


def test_env_treats_empty_values_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("ALLOWED_TELEGRAM_IDS", "1:a")
    monkeypatch.setenv("GROUP_CHAT_ID", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    env = Env(_env_file=None)  # type: ignore[call-arg]
    assert env.group_chat_id is None
    assert env.anthropic_api_key == ""


def test_env_rejects_unknown_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("ALLOWED_TELEGRAM_IDS", "1:a")
    monkeypatch.setenv("TZ", "Mars/Olympus")
    with pytest.raises(ValueError):
        Env(_env_file=None)  # type: ignore[call-arg]
