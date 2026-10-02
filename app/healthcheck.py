"""Container healthcheck (§14.1): ``python -m app.healthcheck``, exit 0 healthy / 1 unhealthy.

With the dashboard on, asks ``/healthz`` (proves the event loop answers and the scheduler
ticks). With it off there's no HTTP server, so it checks the heartbeat file the event loop
touches every minute instead. Reads the environment directly: it must stay cheap and must not
open the database.
"""

from __future__ import annotations

import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from app.config import dashboard_configured
from app.health import HEARTBEAT_FILE

MAX_HEARTBEAT_AGE_S = 180


def check_http(port: int, timeout: float = 5) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=timeout) as r:
            return r.status == 200, f"healthz {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"healthz {e.code}"
    except OSError as e:
        return False, f"healthz unreachable: {e}"


def check_heartbeat(data_dir: Path, max_age_s: float = MAX_HEARTBEAT_AGE_S) -> tuple[bool, str]:
    try:
        age = time.time() - (data_dir / HEARTBEAT_FILE).stat().st_mtime
    except OSError:
        return False, "no heartbeat file"
    return age < max_age_s, f"heartbeat {age:.0f}s old"


def main() -> int:
    env = os.environ
    if dashboard_configured(env.get("DASHBOARD_PASSWORD_HASH", ""), env.get("SESSION_SECRET", "")):
        ok, detail = check_http(int(env.get("DASHBOARD_PORT", "8080")))
    else:
        ok, detail = check_heartbeat(Path(env.get("DATA_DIR", "/data")))
    print(detail)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
