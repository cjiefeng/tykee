"""In-process health signals for the dashboard tiles (§14.3). Not persisted: they describe the
running process. Durable facts (usage, errors in logs) live in SQLite and stdout."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.timeutil import utcnow


@dataclass
class HealthState:
    started_at: datetime = field(default_factory=utcnow)
    last_update_at: datetime | None = None  # any Telegram update reached the gate
    answer_topic_error: str | None = None  # send into the answer topic failed (§10.4)
    answer_topic_error_at: datetime | None = None
    embedder_ok: bool = True
    last_harvest_tick_at: datetime | None = None
    last_llm_ok_at: datetime | None = None
    llm_calls: deque[tuple[datetime, bool]] = field(default_factory=lambda: deque(maxlen=5000))
    budget_dm_day: str | None = None  # household-TZ date the "budget exhausted" DM was sent

    def llm_result(self, ok: bool) -> None:
        now = utcnow()
        self.llm_calls.append((now, ok))
        if ok:
            self.last_llm_ok_at = now

    def llm_error_rate(self, hours: int = 24) -> tuple[int, int]:
        """(errors, calls) in the last ``hours``."""
        since = utcnow() - timedelta(hours=hours)
        recent = [ok for t, ok in self.llm_calls if t >= since]
        return sum(1 for ok in recent if not ok), len(recent)

    def saw_update(self) -> None:
        self.last_update_at = utcnow()

    def topic_error(self, message: str) -> None:
        self.answer_topic_error = message
        self.answer_topic_error_at = utcnow()

    def topic_ok(self) -> None:
        self.answer_topic_error = None
        self.answer_topic_error_at = None
