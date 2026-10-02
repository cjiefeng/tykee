"""Print an argon2id hash for DASHBOARD_PASSWORD_HASH and a random SESSION_SECRET:

docker compose run --rm tykee python -m app.dashboard.hashpw
"""

from __future__ import annotations

import getpass
import secrets

from app.dashboard.core import hash_password


def main() -> None:
    first = getpass.getpass("Dashboard password: ")
    if len(first) < 10:
        raise SystemExit("Use at least 10 characters.")
    if getpass.getpass("Again: ") != first:
        raise SystemExit("Passwords don't match.")
    print("\nAdd these to .env (keep the single quotes: compose interpolates $):\n")
    print(f"DASHBOARD_PASSWORD_HASH='{hash_password(first)}'")
    print(f"SESSION_SECRET='{secrets.token_urlsafe(48)}'")


if __name__ == "__main__":
    main()
