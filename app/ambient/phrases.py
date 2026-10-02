"""Text cues handled in code: mute requests, negative feedback, and /quiet durations."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import timedelta

from app.decisions.text import contains_phrase, normalise

MAX_MUTE = timedelta(days=7)
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)?$")


def matches_any(text: str, phrases: Iterable[str]) -> bool:
    haystack = normalise(text)
    return any(contains_phrase(haystack, normalise(p)) for p in phrases if p.strip())


def parse_duration(arg: str) -> timedelta:
    """'2h', '30m', '90' (minutes), '1d', '1.5 hours'. Raises ValueError if unparseable."""
    m = _DURATION.match(arg.strip().lower())
    if not m:
        raise ValueError(f"can't read duration {arg!r}")
    value = float(m.group(1))
    unit = (m.group(2) or "m")[0]
    delta = {
        "m": timedelta(minutes=value),
        "h": timedelta(hours=value),
        "d": timedelta(days=value),
    }[unit]
    if delta <= timedelta(0):
        raise ValueError("duration must be positive")
    return min(delta, MAX_MUTE)
