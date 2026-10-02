"""The reader's saved Telegram login (§10.7): a Telethon ``StringSession`` encrypted with Fernet
in ``/data/reader.session.enc``. The key (``READER_SESSION_KEY``) lives in ``.env``, outside
``/data``, so backups never contain a usable session."""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SessionKeyError(ValueError):
    """The key is malformed or doesn't decrypt the saved session (lost/rotated key)."""


class SessionVault:
    def __init__(self, path: Path, key: str) -> None:
        self.path = path
        try:
            self._fernet = Fernet(key.encode())
        except (ValueError, TypeError) as e:
            raise SessionKeyError(
                "READER_SESSION_KEY must be a Fernet key (44 characters, URL-safe base64)"
            ) from e

    def exists(self) -> bool:
        return self.path.is_file()

    def mtime(self) -> float | None:
        try:
            return self.path.stat().st_mtime
        except FileNotFoundError:
            return None

    def load(self) -> str | None:
        try:
            token = self.path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            return self._fernet.decrypt(token).decode()
        except InvalidToken as e:
            raise SessionKeyError(
                "the saved reader session can't be decrypted with READER_SESSION_KEY; "
                "log in again with `python -m app.reader.login`"
            ) from e

    def save(self, session: str) -> None:
        """Atomic write, owner-only permissions."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(self._fernet.encrypt(session.encode()))
        tmp.replace(self.path)

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)
