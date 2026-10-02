"""Per-chat debounce (§10.2): fire once the chat has been quiet for ``delay`` seconds.
Pending bursts live in memory only; a restart drops them (the next message starts a new one)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)


class Debouncer:
    def __init__(self, fire: Callable[[int], Awaitable[None]]) -> None:
        self._fire = fire
        self._tasks: dict[int, asyncio.Task[None]] = {}

    def touch(self, key: int, delay: float) -> None:
        self.cancel(key)
        self._tasks[key] = asyncio.create_task(self._wait_then_fire(key, delay))

    def cancel(self, key: int) -> None:
        task = self._tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()

    def pending(self, key: int) -> bool:
        return key in self._tasks

    async def _wait_then_fire(self, key: int, delay: float) -> None:
        await asyncio.sleep(delay)
        # Detach before firing so messages arriving mid-fire start a fresh timer.
        if self._tasks.get(key) is asyncio.current_task():
            del self._tasks[key]
        try:
            await self._fire(key)
        except Exception:
            log.exception("debounced fire failed", extra={"chat_id": key})

    async def close(self) -> None:
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
