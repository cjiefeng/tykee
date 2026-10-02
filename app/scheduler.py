"""Background jobs (§3: APScheduler in the same process). Jobs tick every minute and decide
themselves whether it's time, so intervals in settings are hot-reloaded without rescheduling:
harvester, import, nudges (§10.3), backups (§14.2), embedding retry (§14.4)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.health import HealthState
from app.timeutil import utcnow

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, tz: ZoneInfo, health: HealthState | None = None) -> None:
        self._sched = AsyncIOScheduler(timezone=tz)
        self._health = health

    def every_minute(self, name: str, job: Callable[[], Awaitable[Any]]) -> None:
        self.every(name, job, minutes=1)

    def every(self, name: str, job: Callable[[], Awaitable[Any]], *, minutes: int) -> None:
        async def _run() -> None:
            if self._health is not None:
                self._health.last_tick_at = utcnow()
            try:
                await job()
            except Exception:
                log.exception("scheduled job failed", extra={"job": name})

        self._sched.add_job(
            _run,
            "interval",
            minutes=minutes,
            id=name,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )

    def start(self) -> None:
        self._sched.start()
        log.info("scheduler started", extra={"jobs": [j.id for j in self._sched.get_jobs()]})

    def shutdown(self) -> None:
        if self._sched.running:
            self._sched.shutdown(wait=False)
