"""Structured JSON logging to stdout. Never log secrets or message bodies at INFO."""

from __future__ import annotations

import json
import logging
import sys
from collections import deque
from datetime import UTC, datetime

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in _RESERVED:
                payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class RingBuffer(logging.Handler):
    """Last N formatted log lines for the dashboard's System → logs tail (§11)."""

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self.lines: deque[str] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:  # never let logging break the app
            self.handleError(record)


LOG_BUFFER = RingBuffer()


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    LOG_BUFFER.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler, LOG_BUFFER]
    root.setLevel(level.upper())
    # httpx2: the Anthropic SDK's HTTP client (a line per request); apscheduler: two lines per job
    # run, and the scheduler runs several code-only jobs every minute.
    for noisy in ("httpx", "httpx2", "httpcore", "aiogram.event", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
