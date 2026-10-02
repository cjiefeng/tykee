"""Runs the dashboard inside the bot's event loop (one process, §3)."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator

import uvicorn
from fastapi import FastAPI

log = logging.getLogger(__name__)


class _EmbeddedServer(uvicorn.Server):
    """aiogram owns SIGINT/SIGTERM; the dashboard is stopped via ``should_exit``."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def make_server(app: FastAPI, host: str, port: int) -> uvicorn.Server:
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_config=None,  # our JSON logging stays in charge
        access_log=False,
        lifespan="off",
        proxy_headers=False,  # the client IP must be the real peer for the LAN-only check
        server_header=False,
    )
    return _EmbeddedServer(config)


async def serve(server: uvicorn.Server) -> None:
    """Run the dashboard without ever taking the bot down. uvicorn calls ``sys.exit`` when it
    can't bind its port, which inside a task would end the whole process (Telegram included)."""
    try:
        await server.serve()
    except SystemExit as e:
        log.error("dashboard failed to start; the bot keeps running", extra={"code": e.code})
    except Exception:
        log.exception("dashboard crashed; the bot keeps running")
