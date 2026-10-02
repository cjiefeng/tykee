"""Model escalation for replies (§7.1): Haiku-tier by default, Sonnet-tier ("think") or
Opus-tier ("think even harder") when asked.

Pure code, no extra LLM call: ``/think`` and ``/thinkharder`` force a tier; otherwise phrases
from settings (``escalation.deep_phrases`` before ``escalation.think_phrases``, so "think
harder" isn't caught by "think hard") and a long-message heuristic decide. The orchestrator
drops back to the default tier when spend is past ``budget.warn_ratio``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.settings import RuntimeSettings

Tier = Literal["default", "escalated", "deep"]
TIER_RANK: dict[Tier, int] = {"default": 0, "escalated": 1, "deep": 2}
COMMANDS: dict[str, Tier] = {"think": "escalated", "thinkharder": "deep"}


@dataclass(frozen=True)
class Choice:
    tier: Tier
    reason: str  # 'command' | 'phrase:<p>' | 'long' | 'default' | 'budget'


def _norm(text: str) -> str:
    text = text.casefold().replace("\N{RIGHT SINGLE QUOTATION MARK}", "'")
    return re.sub(r"\s+", " ", text)


def _phrase_in(text: str, phrases: Sequence[str]) -> str | None:
    for p in phrases:
        p = _norm(p).strip()
        if p and re.search(rf"(?<!\w){re.escape(p)}(?!\w)", text):
            return p
    return None


def choose(text: str, s: RuntimeSettings, forced: Tier | None = None) -> Choice:
    """The tier for a reply to ``text``. A forced tier (a command) always wins; phrases can
    still lift ``/think`` to the deep tier ("/think even harder about …")."""
    norm = _norm(text)
    deep = _phrase_in(norm, s.escalation_deep_phrases)
    if forced is not None:
        if forced == "escalated" and deep:
            return Choice("deep", f"phrase:{deep}")
        return Choice(forced, "command")
    if not s.escalation_enabled:
        return Choice("default", "default")
    if deep:
        return Choice("deep", f"phrase:{deep}")
    think = _phrase_in(norm, s.escalation_think_phrases)
    if think:
        return Choice("escalated", f"phrase:{think}")
    limit = s.escalation_long_message_chars
    if limit and len(text.strip()) >= limit:
        return Choice("escalated", "long")
    return Choice("default", "default")
