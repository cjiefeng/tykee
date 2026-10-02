"""In-process health signals for the dashboard tiles (§14.3). Not persisted: they describe the
running process. Durable facts (usage, errors in logs) live in SQLite and stdout."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from app.timeutil import utcnow

log = logging.getLogger(__name__)

HEARTBEAT_FILE = ".heartbeat"


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
    last_tick_at: datetime | None = None  # any scheduler job ran (liveness for /healthz)
    loop_beat: float = field(default_factory=time.monotonic)  # event loop heartbeat (watchdog)
    last_backup_at: datetime | None = None
    last_backup_error: str | None = None
    last_vault_status: str | None = None
    web_rejected: str | None = None  # the API's 400 for a request carrying web tools (§7.5)
    web_rejected_at: datetime | None = None
    reader_state: str = "off"  # §10.7: 'off' | 'not_logged_in' | 'ok' | 'revoked' | 'error'
    reader_error: str | None = None
    reader_last_poll_at: datetime | None = None

    def backup_done(self, vault_status: str) -> None:
        self.last_backup_at = utcnow()
        self.last_backup_error = None
        self.last_vault_status = vault_status

    def backup_failed(self, error: str) -> None:
        self.last_backup_error = error[:300]

    def scheduler_ok(self, max_age_s: float = 300) -> bool:
        """The scheduler ticks every minute; allow a grace period after startup."""
        ref = self.last_tick_at or self.started_at
        return (utcnow() - ref).total_seconds() < max_age_s

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

    def web_rejected_by_api(self, detail: str) -> None:
        self.web_rejected = detail or "request rejected (400)"
        self.web_rejected_at = utcnow()

    def web_ok(self) -> None:
        self.web_rejected = None
        self.web_rejected_at = None

    def saw_update(self) -> None:
        self.last_update_at = utcnow()

    def topic_error(self, message: str) -> None:
        self.answer_topic_error = message
        self.answer_topic_error_at = utcnow()

    def topic_ok(self) -> None:
        self.answer_topic_error = None
        self.answer_topic_error_at = None


async def heartbeat(health: HealthState, beat_file: Path | None, interval_s: float = 15) -> None:
    """Runs on the event loop: proves it's responsive to the watchdog thread, and touches
    ``beat_file`` for the container healthcheck when the dashboard (``/healthz``) is off."""
    last_touch: float | None = None  # monotonic() is time since boot: touch on the first beat
    while True:
        health.loop_beat = time.monotonic()
        if beat_file is not None and (last_touch is None or health.loop_beat - last_touch >= 60):
            try:
                await asyncio.to_thread(beat_file.touch)
                last_touch = health.loop_beat
            except OSError:
                log.warning("heartbeat file not writable", extra={"path": str(beat_file)})
        await asyncio.sleep(interval_s)


class Watchdog:
    """§14.4: if the event loop stops beating for ``stall_s`` (a hang nothing else would notice:
    polling, the scheduler and the dashboard all live on that loop), exit the process so
    Docker's ``restart: unless-stopped`` brings it back. Plain Docker doesn't restart
    *unhealthy* containers, so this is what makes a wedged bot recover unattended."""

    def __init__(
        self,
        health: HealthState,
        stall_s: float = 600,
        check_s: float = 30,
        on_stall: Callable[[], None] | None = None,
    ) -> None:
        self._health = health
        self._stall_s = stall_s
        self._check_s = check_s
        self._on_stall = on_stall or (lambda: os._exit(70))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="watchdog", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._check_s):
            stalled = time.monotonic() - self._health.loop_beat
            if stalled > self._stall_s:
                log.critical("event loop stalled; exiting", extra={"stalled_s": round(stalled)})
                for h in logging.getLogger().handlers:
                    h.flush()
                self._on_stall()
                return
