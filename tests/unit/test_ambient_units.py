"""Stage-1 rules, debounce, phrases and chat_state bookkeeping (§10.2)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.ambient import state as st
from app.ambient.debounce import Debouncer
from app.ambient.phrases import MAX_MUTE, matches_any, parse_duration
from app.ambient.rules import stage1
from app.ambient.state import ChatState
from tests.conftest import NOW, Env

QUIET = ChatState(1, None, None, 0, 1.0)


def _rule(state: ChatState = QUIET, kinds: set[str] | None = None, **kw: object) -> str | None:
    args: dict[str, object] = {"now": NOW, "enabled": True, "cooldown_min": 20, "max_per_day": 5}
    args.update(kw)
    return stage1(state, {"text"} if kinds is None else kinds, **args)  # type: ignore[arg-type]


# --- stage 1 ---------------------------------------------------------------------------------


def test_text_burst_goes_to_judge() -> None:
    assert _rule() is None
    assert _rule(kinds={"text", "sticker"}) is None


def test_disabled() -> None:
    assert _rule(enabled=False) == "disabled"


def test_muted_until_expiry() -> None:
    muted = ChatState(1, NOW + timedelta(minutes=1), None, 0, 1.0)
    assert _rule(muted) == "muted"
    expired = ChatState(1, NOW - timedelta(seconds=1), None, 0, 1.0)
    assert _rule(expired) is None


def test_cooldown_scales_with_multiplier() -> None:
    recent = ChatState(1, None, NOW - timedelta(minutes=19), 1, 1.0)
    assert _rule(recent) == "cooldown"
    assert _rule(ChatState(1, None, NOW - timedelta(minutes=21), 1, 1.0)) is None
    doubled = ChatState(1, None, NOW - timedelta(minutes=30), 1, 2.0)
    assert _rule(doubled) == "cooldown"


def test_daily_cap() -> None:
    assert _rule(ChatState(1, None, None, 5, 1.0)) == "daily_cap"
    assert _rule(ChatState(1, None, None, 4, 1.0)) is None


@pytest.mark.parametrize("kinds", [{"sticker"}, {"photo", "emoji"}, {"voice"}, set()])
def test_media_only_bursts_are_silent(kinds: set[str]) -> None:
    assert _rule(kinds=kinds) == "media_only"


def test_rule_precedence_mute_first() -> None:
    everything = ChatState(1, NOW + timedelta(hours=1), NOW, 9, 1.0)
    assert _rule(everything, kinds={"sticker"}) == "muted"


# --- debounce --------------------------------------------------------------------------------


async def test_debounce_fires_once_after_quiet_period() -> None:
    fired: list[int] = []

    async def fire(key: int) -> None:
        fired.append(key)

    d = Debouncer(fire)
    for _ in range(5):
        d.touch(1, 0.05)
        await asyncio.sleep(0.01)  # a burst: each message resets the timer
    assert fired == []
    await asyncio.sleep(0.08)
    assert fired == [1]
    await d.close()


async def test_debounce_is_per_chat_and_cancellable() -> None:
    fired: list[int] = []

    async def fire(key: int) -> None:
        fired.append(key)

    d = Debouncer(fire)
    d.touch(1, 0.02)
    d.touch(2, 0.02)
    d.cancel(1)
    await asyncio.sleep(0.05)
    assert fired == [2] and not d.pending(1) and not d.pending(2)
    await d.close()


async def test_message_during_fire_starts_new_timer() -> None:
    fired: list[int] = []
    d: Debouncer

    async def fire(key: int) -> None:
        fired.append(key)
        if len(fired) == 1:
            d.touch(key, 0.02)  # a new message arrives while the judge is running

    d = Debouncer(fire)
    d.touch(7, 0.01)
    await asyncio.sleep(0.06)
    assert fired == [7, 7]
    await d.close()


async def test_fire_errors_are_contained() -> None:
    async def boom(key: int) -> None:
        raise RuntimeError("judge exploded")

    d = Debouncer(boom)
    d.touch(1, 0.01)
    await asyncio.sleep(0.03)  # no exception escapes the task
    await d.close()


# --- phrases ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arg", "expected"),
    [
        ("2h", timedelta(hours=2)),
        ("30m", timedelta(minutes=30)),
        ("90", timedelta(minutes=90)),
        ("1d", timedelta(days=1)),
        ("1.5 hours", timedelta(hours=1.5)),
        ("30 mins", timedelta(minutes=30)),
        ("30d", MAX_MUTE),
    ],
)
def test_parse_duration(arg: str, expected: timedelta) -> None:
    assert parse_duration(arg) == expected


@pytest.mark.parametrize("arg", ["", "soon", "-2h", "0", "2 weeks"])
def test_parse_duration_rejects(arg: str) -> None:
    with pytest.raises(ValueError):
        parse_duration(arg)


def test_matches_any_normalises() -> None:
    assert matches_any("lol I didn't ask 😂", ["didn't ask"])
    assert matches_any("OK BOT, SHH!", ["bot shh"])
    assert not matches_any("not nowhere", ["not now"])
    assert not matches_any("anything", ["", "  "])


# --- chat_state ------------------------------------------------------------------------------


async def test_unprompted_counter_resets_daily(env: Env) -> None:
    await env.db.write(lambda c: st.record_unprompted(c, 5, NOW, "2026-10-02"))
    await env.db.write(lambda c: st.record_unprompted(c, 5, NOW, "2026-10-02"))
    s = await env.db.read(lambda c: st.load(c, 5, "2026-10-02"))
    assert s.unprompted_today == 2 and s.last_unprompted_at == NOW
    assert (await env.db.read(lambda c: st.load(c, 5, "2026-10-03"))).unprompted_today == 0
    await env.db.write(lambda c: st.record_unprompted(c, 5, NOW, "2026-10-03"))
    assert (await env.db.read(lambda c: st.load(c, 5, "2026-10-03"))).unprompted_today == 1


async def test_cooldown_multiplier_doubles_and_resets_daily(env: Env) -> None:
    for _ in range(2):
        await env.db.write(lambda c: st.double_cooldown(c, 5, "2026-10-02"))
    assert (await env.db.read(lambda c: st.load(c, 5, "2026-10-02"))).cooldown_multiplier == 4.0
    assert (await env.db.read(lambda c: st.load(c, 5, "2026-10-03"))).cooldown_multiplier == 1.0


async def test_feedback_changes_once(env: Env) -> None:
    log_id = await env.db.write(
        lambda c: st.log(c, chat_id=5, from_msg_id=1, to_msg_id=2, action="respond", now=NOW)
    )
    assert await env.db.write(lambda c: st.set_feedback(c, log_id, "negative"))
    assert not await env.db.write(lambda c: st.set_feedback(c, log_id, "negative"))
