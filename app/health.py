"""In-process health signals for the dashboard tiles (§14.3). Not persisted: they describe the
running process. Durable facts (usage, errors in logs) live in SQLite and stdout."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.timeutil import utcnow


@dataclass
class HealthState:
    started_at: datetime = field(default_factory=utcnow)
    last_update_at: datetime | None = None  # any Telegram update reached the gate
    answer_topic_error: str | None = None  # send into the answer topic failed (§10.4)
    answer_topic_error_at: datetime | None = None
    embedder_ok: bool = True
    last_harvest_tick_at: datetime | None = None

    def saw_update(self) -> None:
        self.last_update_at = utcnow()

    def topic_error(self, message: str) -> None:
        self.answer_topic_error = message
        self.answer_topic_error_at = utcnow()

    def topic_ok(self) -> None:
        self.answer_topic_error = None
        self.answer_topic_error_at = None
