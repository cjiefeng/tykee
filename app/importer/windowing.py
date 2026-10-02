"""Windowing (§15.3): split normalised messages into extraction windows, and estimate what the
import will cost before anything is sent (§15.2 step ⑤). Pure functions."""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.importer.telegram import NormalisedMessage
from app.settings import Pricing

WINDOW_GAP = timedelta(hours=2)
WINDOW_MAX_TOKENS = 6000
OVERLAP = 10  # messages repeated as context at the top of the next window after a size split
MAX_MESSAGE_CHARS = 2000
NEW_MARKER = "--- new ---"

# Output guesses for the estimate: Opus-tier thinks before it answers, and thinking is billed
# as output. Deliberately on the high side.
EXTRACT_OUTPUT_PER_WINDOW = 1500
CONSOLIDATE_INPUT_PER_WINDOW = 500
CONSOLIDATE_OUTPUT_BASE = 12_000
CONSOLIDATE_OUTPUT_PER_WINDOW = 60


def estimate_tokens(text: str) -> int:
    """~3.5 characters per token for Latin text; CJK is about a token per character."""
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    return math.ceil((len(text) - non_ascii) / 3.5 + non_ascii)


def render_line(msg: NormalisedMessage, label: str, tz: ZoneInfo) -> str:
    """``[2026-04-01 19:02] jack: ...``, household-local time, one line per message."""
    text = " / ".join(part.strip() for part in msg.text.splitlines() if part.strip())
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[:MAX_MESSAGE_CHARS] + "…"
    return f"[{msg.ts.astimezone(tz):%Y-%m-%d %H:%M}] {label}: {text}"


@dataclass
class Window:
    chat_ref: str
    ord: int
    start: datetime
    end: datetime
    first_msg_id: int
    last_msg_id: int
    lines: list[str] = field(default_factory=list)
    context: list[str] = field(default_factory=list)
    tokens: int = 0

    @property
    def msg_count(self) -> int:
        return len(self.lines)

    @property
    def text(self) -> str:
        if not self.context:
            return "\n".join(self.lines)
        return "\n".join([*self.context, NEW_MARKER, *self.lines])


def build_windows(
    messages: Iterable[NormalisedMessage],
    labels: Mapping[str, str],
    tz: ZoneInfo,
    *,
    max_tokens: int = WINDOW_MAX_TOKENS,
) -> Iterator[Window]:
    """Messages must arrive grouped by chat in time order (as exports are). A new window starts
    at a new chat, a gap over 2 h, or when the next line would pass ``max_tokens``; after a size
    split the last ``OVERLAP`` lines are repeated as context so a decision spanning the cut is
    still readable. ``labels`` maps sender refs to "jack"/"partner"/"other"."""
    ordinal = 0
    current: Window | None = None
    last_ts: datetime | None = None
    for msg in messages:
        line = render_line(msg, labels.get(msg.sender_ref, "other"), tz)
        tokens = estimate_tokens(line) + 1
        context: list[str] = []
        if current is not None:
            new_chat = msg.chat_ref != current.chat_ref
            gap = last_ts is not None and msg.ts - last_ts > WINDOW_GAP
            full = current.tokens + tokens > max_tokens and current.lines
            if new_chat or gap or full:
                yield current
                if full and not (new_chat or gap):
                    context = current.lines[-OVERLAP:]
                current = None
        if current is None:
            ordinal += 1
            current = Window(
                chat_ref=msg.chat_ref,
                ord=ordinal,
                start=msg.ts,
                end=msg.ts,
                first_msg_id=msg.msg_id,
                last_msg_id=msg.msg_id,
                context=context,
                tokens=sum(estimate_tokens(c) + 1 for c in context),
            )
        current.lines.append(line)
        current.tokens += tokens
        current.end = msg.ts
        current.last_msg_id = msg.msg_id
        last_ts = msg.ts
    if current is not None:
        yield current


def covered(msg: NormalisedMessage, ranges: Mapping[str, Sequence[tuple[int, int]]]) -> bool:
    """True if an earlier import already windowed this (chat, message id) (§15.5 idempotency)."""
    return any(lo <= msg.msg_id <= hi for lo, hi in ranges.get(msg.chat_ref, ()))


@dataclass(frozen=True)
class CostEstimate:
    windows: int
    input_tokens: int
    extract_usd: float
    consolidate_usd: float

    @property
    def total_usd(self) -> float:
        return self.extract_usd + self.consolidate_usd


def estimate_cost(
    window_tokens: Sequence[int],
    system_tokens: int,
    extract_price: Pricing | None,
    consolidate_price: Pricing | None,
    batch_discount: float,
) -> CostEstimate:
    n = len(window_tokens)
    extract_in = sum(window_tokens) + n * system_tokens
    extract_out = n * EXTRACT_OUTPUT_PER_WINDOW
    cons_in = n * CONSOLIDATE_INPUT_PER_WINDOW + 2 * system_tokens
    cons_out = CONSOLIDATE_OUTPUT_BASE + n * CONSOLIDATE_OUTPUT_PER_WINDOW

    def usd(price: Pricing | None, tin: int, tout: int) -> float:
        return 0.0 if price is None else (tin * price.input + tout * price.output) / 1_000_000

    return CostEstimate(
        windows=n,
        input_tokens=extract_in,
        extract_usd=batch_discount * usd(extract_price, extract_in, extract_out),
        consolidate_usd=usd(consolidate_price, cons_in, cons_out),
    )
