"""One-time shell login for the account reader (§10.7):

    docker exec -it tykee python -m app.reader.login

Asks for the phone number, the login code and the 2FA password (passed to Telegram, never
stored), then saves the encrypted session to ``/data/reader.session.enc``. The running bot picks
it up on its next tick; turn the reader on and add chats in the dashboard (System → Account
reader).
"""

from __future__ import annotations

import asyncio
import getpass
import sqlite3
import sys

from app.config import Env
from app.db.database import Database
from app.reader.service import audit
from app.reader.session import SessionKeyError, SessionVault
from app.reader.telethon_reader import DEVICE_MODEL, interactive_login


async def _login(env: Env, vault: SessionVault, api_id: int) -> int:
    session = await interactive_login(
        api_id,
        env.tg_api_hash,
        phone=lambda: input("Phone number (international format, e.g. +65...): ").strip(),
        code=lambda: input("Login code (sent to your Telegram app): ").strip(),
        password=lambda: getpass.getpass("2FA password (not stored): "),
    )
    vault.save(session)
    db = Database(env.db_path)
    await db.open()
    try:

        def _audit(c: sqlite3.Connection) -> None:
            audit(c, "login", None, "system", "shell login")

        await db.write(_audit)
    finally:
        await db.close()
    print(
        f"Saved. It shows up in Telegram → Settings → Devices as “{DEVICE_MODEL}”.\n"
        "Next: dashboard → System → Account reader: turn it on, add chats, tick consent."
    )
    return 0


def main() -> None:
    env = Env()
    if not env.reader_configured or env.tg_api_id is None:
        print("Set TG_API_ID, TG_API_HASH and READER_SESSION_KEY in .env first (see README).")
        sys.exit(1)
    try:
        vault = SessionVault(env.reader_session_path, env.reader_session_key)
    except SessionKeyError as e:
        print(e)
        sys.exit(1)
    if vault.exists():
        answer = input("A reader session is already saved. Replace it? [y/N] ").strip().lower()
        if answer != "y":
            sys.exit(0)
    sys.exit(asyncio.run(_login(env, vault, env.tg_api_id)))


if __name__ == "__main__":
    main()
