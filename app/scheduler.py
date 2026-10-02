"""Background jobs (§3: APScheduler in the same process). Jobs tick every minute and decide
themselves whether it's time, so intervals in settings are hot-reloaded without rescheduling.
M6 adds nudges and backups here."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, tz: ZoneInfo) -> None:
        self._sched = AsyncIOScheduler(timezone=tz)

    def every_minute(self, name: str, job: Callable[[], Awaitable[Any]]) -> None:
        async def _run() -> None:
            try:
                await job()
            except Exception:
                log.exception("scheduled job failed", extra={"job": name})

        self._sched.add_job(
            _run,
            "interval",
            minutes=1,
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
