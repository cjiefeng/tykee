"""Stage-1 hard rules (§10.2): cheap checks in code before any LLM call. Direct mentions,
replies and commands never reach here; they always get an answer."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime, timedelta

from app.ambient.state import ChatState

NON_TEXT_KINDS = frozenset({"sticker", "photo", "emoji", "voice", "other"})


def stage1(
    state: ChatState,
    burst_kinds: Collection[str],
    *,
    now: datetime,
    enabled: bool,
    cooldown_min: float,
    max_per_day: int,
) -> str | None:
    """Name of the rule that forces silence, or None if the burst should go to the judge."""
    if not enabled:
        return "disabled"
    if state.muted_until is not None and now < state.muted_until:
        return "muted"
    if state.last_unprompted_at is not None and now - state.last_unprompted_at < timedelta(
        minutes=cooldown_min * state.cooldown_multiplier
    ):
        return "cooldown"
    if state.unprompted_today >= max_per_day:
        return "daily_cap"
    if not burst_kinds or set(burst_kinds) <= NON_TEXT_KINDS:
        return "media_only"
    return None
