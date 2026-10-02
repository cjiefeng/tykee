"""``ReaderService`` (§10.7): runs the account reader and manages its chat allowlist.

Every minute ``tick()`` (pure code) polls each enabled, consented chat whose interval is due:
one ``fetch_new`` per chat, new messages stored in ``messages`` with ``source='account_reader'``
(senders mapped to users, everyone else to NULL = "other"; Maps links annotated like the bot's
own messages), cursor advanced, and the harvester run on what's new. Raw text is deleted
``retention_days`` after it has been harvested; only extracted decisions, approved facts and
places remain.

The dashboard calls the rest: add/remove/update chats, consent, the master switch, Disconnect
(the kill switch) and Backfill (history → a Telegram-export-shaped file → the import wizard).
Every change is written to ``reader_audit``; adding a chat or giving consent DMs the admin.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from io import BytesIO
from typing import Any, Literal
from zoneinfo import ZoneInfo

from app.db.database import Database
from app.db.repos import messages as messages_repo
from app.db.repos.users import UserRecord
from app.health import HealthState
from app.importer.service import ImportService
from app.places.links import LOCATION_SHARED
from app.places.service import PlaceService
from app.reader import chats
from app.reader.models import (
    ChatInfo,
    ChatRef,
    DialogName,
    Reader,
    ReaderChatNotFound,
    ReaderError,
    ReaderFloodWait,
    ReaderMessage,
    ReaderNotAllowed,
    ReaderRevoked,
)
from app.reader.session import SessionKeyError, SessionVault
from app.settings import SettingsStore, set_value
from app.timeutil import from_sql, to_sql, utcnow

log = logging.getLogger(__name__)

Connect = Callable[[str], Awaitable[Reader]]
Alert = Callable[[str], Awaitable[None]]
Actor = Literal["dashboard", "system"]

PURGE_EVERY = timedelta(hours=1)
BACKFILL_DAYS = 183  # "up to 6 months", same default range as the import wizard


class ReaderProblem(ValueError):
    """Shown to the admin as is."""


@dataclass(frozen=True)
class ReaderChat:
    id: int
    peer_id: int
    access_hash: int | None
    kind: str
    thread_id: int | None
    label: str
    enabled: bool
    consent_at: str | None
    interval_min: int
    retention_days: int
    last_msg_id: int
    baseline_msg_id: int | None
    last_fetch_at: str | None
    fetch_day: str | None
    fetched_today: int
    last_harvest_at: str | None
    last_harvest: str | None
    status: str
    error: str | None

    @property
    def ref(self) -> ChatRef:
        return ChatRef(self.peer_id, self.access_hash, self.thread_id)

    @property
    def readable(self) -> bool:
        return self.enabled and self.consent_at is not None


def _chat(r: sqlite3.Row) -> ReaderChat:
    return ReaderChat(
        id=r["id"],
        peer_id=r["peer_id"],
        access_hash=r["access_hash"],
        kind=r["kind"],
        thread_id=r["thread_id"],
        label=r["label"],
        enabled=bool(r["enabled"]),
        consent_at=r["consent_at"],
        interval_min=r["interval_min"],
        retention_days=r["retention_days"],
        last_msg_id=r["last_msg_id"],
        baseline_msg_id=r["baseline_msg_id"],
        last_fetch_at=r["last_fetch_at"],
        fetch_day=r["fetch_day"],
        fetched_today=r["fetched_today"],
        last_harvest_at=r["last_harvest_at"],
        last_harvest=r["last_harvest"],
        status=r["status"],
        error=r["error"],
    )


@dataclass
class Backfill:
    chat_label: str
    status: str  # 'running' | 'done' | 'failed'
    messages: int = 0
    job_id: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class PollResult:
    chat_id: int
    status: str
    stored: int = 0


def audit(
    c: sqlite3.Connection, action: str, label: str | None, actor: Actor, detail: str = ""
) -> None:
    c.execute(
        "INSERT INTO reader_audit(action, chat_label, actor, detail) VALUES (?, ?, ?, ?)",
        (action, label, actor, detail or None),
    )


class ReaderService:
    def __init__(
        self,
        *,
        db: Database,
        settings: SettingsStore,
        users: Sequence[UserRecord],
        tz: ZoneInfo,
        vault: SessionVault | None,
        connect: Connect | None,
        alert: Alert,
        group_id: Callable[[], int | None],
        harvest: Callable[[], Awaitable[Any]] | None = None,
        places: PlaceService | None = None,
        importer: ImportService | None = None,
        health: HealthState | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._settings = settings
        self._users_by_tg = {u.telegram_id: u for u in users}
        self._tz = tz
        self._vault = vault
        self._connect = connect
        self._alert = alert
        self._group_id = group_id
        self._harvest = harvest
        self._places = places
        self._importer = importer
        self._health = health
        self._clock = clock
        self._reader: Reader | None = None
        self._session_mtime: float | None = None
        self._paused_until: datetime | None = None  # FloodWait is account-wide
        self._last_purge: datetime | None = None
        self._lock = asyncio.Lock()
        self.backfills: dict[int, Backfill] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    # --- state -------------------------------------------------------------------------------

    @property
    def configured(self) -> bool:
        """``TG_API_ID``, ``TG_API_HASH`` and ``READER_SESSION_KEY`` are set."""
        return self._vault is not None and self._connect is not None

    @property
    def logged_in(self) -> bool:
        return self._vault is not None and self._vault.exists()

    def _set_health(self, state: str, error: str | None = None) -> None:
        if self._health is not None:
            self._health.reader_state = state
            self._health.reader_error = error

    async def chats(self) -> list[ReaderChat]:
        rows = await self._db.read(
            lambda c: c.execute("SELECT * FROM reader_chats ORDER BY id").fetchall()
        )
        return [_chat(r) for r in rows]

    async def chat(self, chat_id: int) -> ReaderChat:
        row = await self._db.read(
            lambda c: c.execute("SELECT * FROM reader_chats WHERE id = ?", (chat_id,)).fetchone()
        )
        if row is None:
            raise ReaderProblem("no such reader chat")
        return _chat(row)

    async def audit_log(self, limit: int = 50) -> list[sqlite3.Row]:
        return await self._db.read(
            lambda c: c.execute(
                "SELECT * FROM reader_audit ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        )

    # --- connection --------------------------------------------------------------------------

    async def _client(self) -> Reader:
        """The connected reader, (re)connecting when the saved session appeared or changed
        (the shell login needs no restart)."""
        if self._vault is None or self._connect is None:
            raise ReaderProblem(
                "the account reader isn't configured (TG_API_ID, TG_API_HASH, READER_SESSION_KEY)"
            )
        mtime = self._vault.mtime()
        if self._reader is not None and mtime == self._session_mtime:
            return self._reader
        await self._drop_client()
        try:
            session = self._vault.load()
        except SessionKeyError as e:
            self._set_health("error", str(e))
            raise ReaderProblem(str(e)) from e
        if not session:
            self._set_health("not_logged_in")
            raise ReaderProblem("not logged in yet: run `python -m app.reader.login` on the NAS")
        try:
            self._reader = await self._connect(session)
        except ReaderRevoked as e:
            await self._revoked(str(e))
            raise ReaderProblem("Telegram revoked the reader session; log in again") from e
        self._session_mtime = mtime
        return self._reader

    async def _drop_client(self) -> None:
        if self._reader is not None:
            try:
                await self._reader.close()
            except Exception:
                log.exception("reader close failed")
            self._reader = None

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await self._drop_client()

    async def _revoked(self, reason: str) -> None:
        """§10.7: the reader disables itself, shows a red tile and DMs the admin."""
        await self._drop_client()
        if self._vault is not None:
            self._vault.delete()
        self._session_mtime = None

        def _off(c: sqlite3.Connection) -> None:
            set_value(c, "reader.enabled", False)
            c.execute("UPDATE reader_chats SET status = 'revoked'")
            audit(c, "revoked", None, "system", reason[:200])

        await self._db.write(_off)
        self._set_health("revoked", "Telegram ended the reader session")
        log.warning("reader session revoked", extra={"reason": reason[:200]})
        await self._safe_alert(
            "Tykee reader: Telegram ended the read-only session, so the reader turned itself off. "
            "Log in again on the NAS to resume."
        )

    async def _safe_alert(self, text: str) -> None:
        try:
            await self._alert(text)
        except Exception:
            log.exception("reader alert failed")

    # --- polling (scheduler) -----------------------------------------------------------------

    async def tick(self) -> list[PollResult]:
        """Every minute: poll chats whose interval is due. Code only, no LLM."""
        async with self._lock:
            return await self._tick()

    async def _tick(self) -> list[PollResult]:
        s = await self._settings.load()
        now = self._clock()
        if self._last_purge is None or now - self._last_purge >= PURGE_EVERY:
            await self.purge()
        if not self.configured:
            self._set_health("off")
            return []
        if not s.reader_enabled:
            await self._drop_client()
            self._set_health("off" if self.logged_in else "not_logged_in")
            return []
        if self._paused_until is not None and now < self._paused_until:
            return []
        due = [
            ch
            for ch in await self.chats()
            if ch.readable
            and (
                ch.last_fetch_at is None
                or now - from_sql(ch.last_fetch_at) >= timedelta(minutes=ch.interval_min)
            )
        ]
        if not due:
            return []
        try:
            reader = await self._client()
        except ReaderProblem:
            return []
        results: list[PollResult] = []
        for ch in due:
            res = await self._poll(reader, ch)
            results.append(res)
            if res.status == "revoked":
                return results
            if res.status == "flood_wait":
                break
        if self._health is not None:
            self._health.reader_last_poll_at = now
        if any(r.status == "ok" for r in results):
            self._set_health("ok")
        if any(r.stored for r in results) and self._harvest is not None:
            try:
                await self._harvest()
            except Exception:
                log.exception("reader harvest failed")
        return results

    async def _poll(self, reader: Reader, ch: ReaderChat) -> PollResult:
        now = self._clock()
        try:
            start = None if ch.baseline_msg_id is None else ch.last_msg_id
            batch = await reader.fetch_new(ch.ref, start)
        except ReaderRevoked as e:
            await self._revoked(str(e))
            return PollResult(ch.id, "revoked")
        except ReaderFloodWait as e:
            self._paused_until = now + timedelta(seconds=e.seconds)
            await self._status(ch, "flood_wait", str(e))
            return PollResult(ch.id, "flood_wait")
        except ReaderChatNotFound as e:
            await self._status(ch, "not_found", str(e))
            return PollResult(ch.id, "not_found")
        except ReaderNotAllowed as e:
            await self._status(ch, "error", str(e))
            return PollResult(ch.id, "error")
        except ReaderError as e:
            await self._status(ch, "error", str(e))
            self._set_health("error", str(e))
            return PollResult(ch.id, "error")
        if ch.baseline_msg_id is None:
            # First poll: start from the newest message; older history is the Backfill's job.
            start = max((m.id for m in batch), default=0)
            await self._db.write(
                lambda c: c.execute(
                    "UPDATE reader_chats SET baseline_msg_id = ?, last_msg_id = ?, "
                    "last_fetch_at = ?, status = 'ok', error = NULL WHERE id = ?",
                    (start, start, to_sql(now), ch.id),
                )
            )
            return PollResult(ch.id, "ok")
        rows = [await self._prepare(ch, m) for m in batch if m.text]
        cursor = max((m.id for m in batch), default=ch.last_msg_id)
        today = now.astimezone(self._tz).date().isoformat()

        def _save(c: sqlite3.Connection) -> int:
            stored = 0
            for row in rows:
                if messages_repo.insert(c, **row) is not None:
                    stored += 1
            fetched = (ch.fetched_today if ch.fetch_day == today else 0) + stored
            c.execute(
                "UPDATE reader_chats SET last_msg_id = MAX(last_msg_id, ?), last_fetch_at = ?, "
                "fetch_day = ?, fetched_today = ?, status = 'ok', error = NULL WHERE id = ?",
                (cursor, to_sql(now), today, fetched, ch.id),
            )
            return stored

        stored = await self._db.write(_save)
        if stored:
            log.info("reader fetched", extra={"reader_chat": ch.id, "messages": stored})
        return PollResult(ch.id, "ok", stored)

    async def _prepare(self, ch: ReaderChat, m: ReaderMessage) -> dict[str, Any]:
        text = m.text
        if self._places is not None and (await self._settings.load()).places_enabled:
            try:
                if m.venue is not None:
                    v = m.venue
                    text = (
                        await self._places.annotate_venue(
                            text, v.title, v.address, v.lat, v.lng, v.google_place_id
                        )
                    ).text
                elif m.location:
                    text = f"{text} {LOCATION_SHARED}"
                elif m.urls:
                    text = (await self._places.annotate_urls(text, list(m.urls))).text
            except Exception:
                log.exception("reader place annotation failed")
        user = self._users_by_tg.get(m.sender_id) if m.sender_id is not None else None
        return {
            "chat_id": ch.peer_id,
            "tg_message_id": m.id,
            "user_id": user.id if user else None,  # not a Tykee user → "other"
            "role": "user",
            "kind": m.kind,
            "content": messages_repo.text_content(text),
            "thread_id": ch.thread_id,
            "source": messages_repo.READER,
            "created_at": to_sql(m.date),
        }

    async def _status(self, ch: ReaderChat, status: str, error: str) -> None:
        now = to_sql(self._clock())
        await self._db.write(
            lambda c: c.execute(
                "UPDATE reader_chats SET status = ?, error = ?, last_fetch_at = ? WHERE id = ?",
                (status, error[:300], now, ch.id),
            )
        )
        log.warning("reader poll failed", extra={"reader_chat": ch.id, "status": status})

    async def purge(self) -> int:
        """Retention (§10.7): delete raw text ``retention_days`` after it was harvested."""
        self._last_purge = self._clock()
        now = self._clock()

        def _q(c: sqlite3.Connection) -> int:
            deleted = 0
            for r in c.execute("SELECT * FROM reader_chats").fetchall():
                cutoff = to_sql(now - timedelta(days=int(r["retention_days"])))
                cur = c.execute(
                    "DELETE FROM messages WHERE source = 'account_reader' AND chat_id = ? "
                    "AND thread_id IS ? AND id <= ? AND created_at < ?",
                    (r["peer_id"], r["thread_id"], r["harvest_msg_id"], cutoff),
                )
                deleted += cur.rowcount
            return deleted

        deleted = await self._db.write(_q)
        if deleted:
            log.info("reader retention", extra={"deleted": deleted})
        return deleted

    # --- allowlist (dashboard) ---------------------------------------------------------------

    async def set_enabled(self, on: bool) -> None:
        if on and not self.logged_in:
            raise ReaderProblem("log in first: `python -m app.reader.login` on the NAS")

        def _set(c: sqlite3.Connection) -> None:
            set_value(c, "reader.enabled", on)
            audit(c, "reader_on" if on else "reader_off", None, "dashboard")

        await self._db.write(_set)
        if not on:
            await self._drop_client()
            self._set_health("off")

    async def resolve(self, raw: str, topic: int | None) -> ChatInfo:
        """Resolve what the admin typed and apply the guard rails. Metadata only."""
        try:
            ref = chats.parse_ref(raw)
        except chats.ChatRefError as e:
            raise ReaderProblem(str(e)) from e
        reader = await self._client()
        try:
            info = await reader.resolve(ref)
        except ReaderRevoked as e:
            await self._revoked(str(e))
            raise ReaderProblem("Telegram revoked the reader session") from e
        except ReaderChatNotFound as e:
            raise ReaderProblem(f"couldn't find that chat ({e})") from e
        except ReaderError as e:
            raise ReaderProblem(str(e)) from e
        s = await self._settings.load()
        why = chats.refusal(
            info, group_id=self._group_id(), max_members=s.reader_max_group_members, topic=topic
        )
        if why:
            raise ReaderProblem(why)
        return info

    async def add(
        self, info: ChatInfo, *, label: str, topic: int | None, consent: bool
    ) -> ReaderChat:
        """Called after step-up auth. Topic rows and a whole-group row of the same chat would
        read the same messages twice, so they exclude each other."""
        s = await self._settings.load()
        kind = "user" if info.kind == "user" else ("topic" if topic is not None else "group")
        name = (label.strip() or info.title)[:80]
        now = to_sql(self._clock())

        def _ins(c: sqlite3.Connection) -> int:
            same = c.execute(
                "SELECT thread_id FROM reader_chats WHERE peer_id = ?", (info.peer_id,)
            ).fetchall()
            if any(r["thread_id"] == topic for r in same):
                raise ReaderProblem("that chat is already on the list")
            if same and (topic is None or any(r["thread_id"] is None for r in same)):
                raise ReaderProblem(
                    "that group is already on the list (whole group and single topics exclude "
                    "each other)"
                )
            cur = c.execute(
                "INSERT INTO reader_chats(peer_id, access_hash, kind, thread_id, label, "
                "consent_at, interval_min, retention_days) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    info.peer_id,
                    info.access_hash,
                    kind,
                    topic,
                    name,
                    now if consent else None,
                    s.reader_default_interval_min,
                    s.reader_default_retention_days,
                ),
            )
            audit(c, "add", name, "dashboard", f"{kind} {info.peer_id}")
            if consent:
                audit(c, "consent", name, "dashboard")
            return int(cur.lastrowid or 0)

        chat_id = await self._db.write(_ins)
        log.info("reader chat added", extra={"reader_chat": chat_id, "kind": kind})
        await self._safe_alert(f"Tykee reader: '{name}' added to readable chats")
        return await self.chat(chat_id)

    async def update(
        self, chat_id: int, *, label: str, enabled: bool, interval_min: int, retention_days: int
    ) -> None:
        if not 5 <= interval_min <= 1440:
            raise ReaderProblem("the polling interval must be 5 to 1440 minutes")
        if not 1 <= retention_days <= 90:
            raise ReaderProblem("retention must be 1 to 90 days")
        ch = await self.chat(chat_id)
        name = (label.strip() or ch.label)[:80]

        def _upd(c: sqlite3.Connection) -> None:
            c.execute(
                "UPDATE reader_chats SET label = ?, enabled = ?, interval_min = ?, "
                "retention_days = ? WHERE id = ?",
                (name, int(enabled), interval_min, retention_days, chat_id),
            )
            if enabled != ch.enabled:
                audit(c, "enable" if enabled else "disable", name, "dashboard")
            if name != ch.label:
                audit(c, "rename", name, "dashboard", f"was {ch.label}")

        await self._db.write(_upd)

    async def set_consent(self, chat_id: int, consent: bool) -> None:
        """Ticking needs step-up auth (checked by the dashboard); unticking stops reading at
        once (``fetch_new`` re-checks the row on every call)."""
        ch = await self.chat(chat_id)
        now = to_sql(self._clock())

        def _set(c: sqlite3.Connection) -> None:
            c.execute(
                "UPDATE reader_chats SET consent_at = ? WHERE id = ?",
                (now if consent else None, chat_id),
            )
            audit(c, "consent" if consent else "unconsent", ch.label, "dashboard")

        await self._db.write(_set)
        if consent:
            await self._safe_alert(f"Tykee reader: consent recorded for '{ch.label}'")

    async def remove(self, chat_id: int) -> int:
        """Stops reading and deletes the chat's raw text (harvested rows too: without the row
        the retention job couldn't find them). Extracted decisions, facts and places stay."""
        ch = await self.chat(chat_id)

        def _del(c: sqlite3.Connection) -> int:
            cur = c.execute(
                "DELETE FROM messages WHERE source = 'account_reader' AND chat_id = ? "
                "AND thread_id IS ?",
                (ch.peer_id, ch.thread_id),
            )
            c.execute("DELETE FROM reader_chats WHERE id = ?", (chat_id,))
            audit(c, "remove", ch.label, "dashboard", f"{cur.rowcount} raw messages deleted")
            return cur.rowcount

        deleted = await self._db.write(_del)
        log.info("reader chat removed", extra={"reader_chat": chat_id, "deleted": deleted})
        return deleted

    async def dialogs(self) -> list[DialogName]:
        """ "Pick from my chats": fetched on demand, never stored."""
        reader = await self._client()
        try:
            return await reader.list_dialog_names(50)
        except ReaderRevoked as e:
            await self._revoked(str(e))
            raise ReaderProblem("Telegram revoked the reader session") from e
        except ReaderError as e:
            raise ReaderProblem(str(e)) from e

    async def disconnect(self) -> str:
        """Kill switch: log out on Telegram (the session leaves Settings → Devices), delete the
        encrypted file, turn the reader off."""
        outcome = "no session was saved"
        if self.logged_in:
            try:
                reader = await self._client()
                await reader.log_out()
                outcome = "logged out on Telegram"
            except (ReaderProblem, ReaderError) as e:
                outcome = f"Telegram log-out failed ({e}); terminate it under Settings → Devices"
        await self._drop_client()
        if self._vault is not None:
            self._vault.delete()
        self._session_mtime = None

        def _off(c: sqlite3.Connection) -> None:
            set_value(c, "reader.enabled", False)
            audit(c, "logout", None, "dashboard", outcome)

        await self._db.write(_off)
        self._set_health("not_logged_in")
        log.warning("reader disconnected", extra={"outcome": outcome})
        return outcome

    # --- backfill → import (§15) ---------------------------------------------------------------

    def start_backfill(self, chat_id: int) -> None:
        if self._importer is None:
            raise ReaderProblem("the import pipeline isn't running")
        if any(b.status == "running" for b in self.backfills.values()):
            raise ReaderProblem("a backfill is already running")
        task = asyncio.create_task(self._backfill(chat_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _backfill(self, chat_id: int) -> None:
        ch = await self.chat(chat_id)
        state = self.backfills[chat_id] = Backfill(ch.label, "running")
        try:
            job_id = await self.backfill(ch, state)
            state.status, state.job_id = "done", job_id
        except Exception as e:
            state.status, state.error = "failed", str(e)[:300]
            log.warning("reader backfill failed", extra={"reader_chat": chat_id})

    async def backfill(self, ch: ReaderChat, state: Backfill | None = None) -> int:
        """Pull up to 6 months of history (older than where live reading started) into a
        Telegram-export-shaped file and hand it to the import wizard: same configure, cost
        preview, consent and review steps as a manual export."""
        if not ch.readable:
            raise ReaderProblem("tick consent and enable the chat first")
        if self._importer is None:
            raise ReaderProblem("the import pipeline isn't running")
        reader = await self._client()
        since = self._clock() - timedelta(days=BACKFILL_DAYS)
        try:
            history = await reader.backfill(ch.ref, since, ch.baseline_msg_id)
        except ReaderRevoked as e:
            await self._revoked(str(e))
            raise ReaderProblem("Telegram revoked the reader session") from e
        except ReaderError as e:
            raise ReaderProblem(str(e)) from e
        if state is not None:
            state.messages = len(history)
        if not history:
            raise ReaderProblem("no messages in the last 6 months")
        export = self._export(ch, history)

        def _audit(c: sqlite3.Connection) -> None:
            audit(c, "backfill", ch.label, "dashboard", f"{len(history)} messages → import")

        job_id = await self._importer.receive(
            BytesIO(json.dumps(export, ensure_ascii=False).encode()),
            f"reader-{ch.label}.json",
        )
        await self._db.write(_audit)
        return job_id

    def _export(self, ch: ReaderChat, history: Sequence[ReaderMessage]) -> dict[str, Any]:
        """Single-chat Telegram Desktop export shape (§15.1), so the importer needs no new
        parser. Senders are ``user<id>`` like a real export, so the wizard maps them."""
        names = {u.telegram_id: u.display_name for u in self._users_by_tg.values()}
        return {
            "name": ch.label,
            "type": "personal_chat" if ch.kind == "user" else "private_group",
            "id": ch.peer_id,
            "messages": [
                {
                    "id": m.id,
                    "type": "message",
                    "date": m.date.astimezone(self._tz).replace(tzinfo=None).isoformat(),
                    "date_unixtime": str(int(m.date.timestamp())),
                    "from": names.get(m.sender_id or 0, "someone"),
                    "from_id": f"user{m.sender_id}" if m.sender_id else "",
                    "text": m.text,
                }
                for m in history
            ],
        }
