"""Nightly backups (§14.2): a consistent online copy of the DB (`VACUUM INTO`, keep
`backup.keep`) and a commit of the vault into its own local git repo (no remote, ever).

The scheduler ticks every minute; the backup runs once per household day, at or after
`backup.time`, so a NAS that was off at 03:00 catches up when it's back. "Done today" is simply
"today's file exists", which survives restarts without extra state.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.db.database import Database
from app.health import HealthState
from app.settings import SettingsStore
from app.timeutil import utcnow

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^bot-(\d{8})\.db$")
GIT_IDENTITY = ["-c", "user.name=Tykee", "-c", "user.email=tykee@localhost"]


@dataclass(frozen=True)
class BackupFile:
    name: str
    path: Path
    size: int
    modified: datetime


@dataclass(frozen=True)
class BackupResult:
    db_file: str
    pruned: list[str]
    vault: str  # 'committed' | 'unchanged' | 'skipped: …' | 'failed: …'


def list_backups(backup_dir: Path) -> list[BackupFile]:
    """Newest first."""
    if not backup_dir.is_dir():
        return []
    out = []
    for p in backup_dir.iterdir():
        if NAME_RE.match(p.name) and p.is_file():
            st = p.stat()
            out.append(
                BackupFile(p.name, p, st.st_size, datetime.fromtimestamp(st.st_mtime).astimezone())
            )
    return sorted(out, key=lambda b: b.name, reverse=True)


def prune(backup_dir: Path, keep: int) -> list[str]:
    removed = []
    for b in list_backups(backup_dir)[keep:]:
        b.path.unlink(missing_ok=True)
        removed.append(b.name)
    return removed


def _vacuum_into(conn: sqlite3.Connection, target: Path) -> None:
    """On a pool reader: ``query_only`` would refuse VACUUM INTO (it writes the *target*), so
    lift it for this statement; the connection is opened ``mode=ro``, so bot.db stays safe."""
    conn.execute("PRAGMA query_only = OFF")
    try:
        conn.execute("VACUUM INTO ?", (str(target),))
    finally:
        conn.execute("PRAGMA query_only = ON")


async def _git(vault: Path, *args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=vault,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace").strip()


async def commit_vault(vault: Path, message: str) -> str:
    if shutil.which("git") is None:
        return "skipped: git not installed"
    is_dir, has_repo = await asyncio.to_thread(lambda: (vault.is_dir(), (vault / ".git").exists()))
    if not is_dir:
        return "skipped: no vault"
    if not has_repo:
        code, out = await _git(vault, "init", "-q", "-b", "main")
        if code:
            return f"failed: git init: {out[:200]}"
    code, remotes = await _git(vault, "remote")
    if remotes:
        # §12: the vault repo must never have a remote. Refuse rather than silently carry on.
        log.error("vault git repo has a remote; refusing to commit", extra={"remotes": remotes})
        return "failed: vault repo has a remote configured (remove it)"
    code, out = await _git(vault, "add", "-A")
    if code:
        return f"failed: git add: {out[:200]}"
    code, _ = await _git(vault, "diff", "--cached", "--quiet")
    if code == 0:
        return "unchanged"
    code, out = await _git(vault, *GIT_IDENTITY, "commit", "-q", "-m", message)
    return "committed" if code == 0 else f"failed: git commit: {out[:200]}"


class BackupService:
    def __init__(
        self,
        *,
        db: Database,
        settings: SettingsStore,
        backup_dir: Path,
        vault: Path,
        tz: ZoneInfo,
        health: HealthState | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._settings = settings
        self.backup_dir = backup_dir
        self._vault = vault
        self._tz = tz
        self._health = health
        self._clock = clock
        self._lock = asyncio.Lock()

    def _name_for(self, now: datetime) -> str:
        return f"bot-{now.astimezone(self._tz):%Y%m%d}.db"

    async def tick(self) -> BackupResult | None:
        s = await self._settings.load()
        if not s.backup_enabled:
            return None
        now = self._clock()
        local = now.astimezone(self._tz)
        if f"{local:%H:%M}" < s.backup_time:
            return None
        if await asyncio.to_thread((self.backup_dir / self._name_for(now)).exists):
            return None
        return await self.run()

    async def run(self) -> BackupResult:
        """Back up now (also System → "Back up now"); replaces today's file if there is one."""
        async with self._lock:
            try:
                result = await self._run()
            except Exception as e:
                log.exception("backup failed")
                if self._health is not None:
                    self._health.backup_failed(str(e))
                raise
        if self._health is not None:
            self._health.backup_done(result.vault)
        log.info(
            "backup done",
            extra={"file": result.db_file, "pruned": len(result.pruned), "vault": result.vault},
        )
        return result

    async def _run(self) -> BackupResult:
        s = await self._settings.load()
        now = self._clock()
        name = self._name_for(now)
        tmp = self.backup_dir / f".{name}.tmp"

        def _prepare() -> None:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            tmp.unlink(missing_ok=True)

        def _finish() -> list[str]:
            tmp.replace(self.backup_dir / name)
            return prune(self.backup_dir, s.backup_keep)

        await asyncio.to_thread(_prepare)
        # A read-only connection is enough for VACUUM INTO and keeps the writer thread free.
        await self._db.read(lambda c: _vacuum_into(c, tmp))
        pruned = await asyncio.to_thread(_finish)
        local = now.astimezone(self._tz)
        vault = await commit_vault(self._vault, f"nightly {local:%Y-%m-%d %H:%M}")
        return BackupResult(name, pruned, vault)
