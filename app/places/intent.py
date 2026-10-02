"""Code-only intent for shared places (§10.5): "eating here" + a Maps link in the answer topic
is a decision, without an LLM call. The category comes from the message itself ("dinner here")
or from the household-time meal slot. Pure functions."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import time

from app.ambient.phrases import matches_any
from app.places.links import strip_markup
from app.settings import MealSlot


def has_intent(text: str, phrases: Sequence[str]) -> bool:
    return matches_any(strip_markup(text), phrases)


def _t(hhmm: str) -> time:
    hh, mm = hhmm.split(":")
    return time(int(hh), int(mm))


def slot_category(local: time, slots: Sequence[MealSlot]) -> str | None:
    """The first slot containing ``local`` (start inclusive, end exclusive)."""
    for slot in slots:
        start, end = _t(slot.start), _t(slot.end)
        inside = start <= local < end if start <= end else (local >= start or local < end)
        if inside:
            return slot.category
    return None
