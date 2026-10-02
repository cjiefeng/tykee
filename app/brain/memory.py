"""Memory service (§6.5-6.7): what Claude's memory tools, the prompt builder, the decision
engine and (from M4) the topic harvester use. Wraps NoteStore + Retriever with the owner and
write policies, and owns the memory inbox."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from app.brain import notes as nt
from app.brain.notes import Note, PathError
from app.brain.retrieval import Hit, Retriever
from app.brain.store import NoteStore, WriteMode, WriteResult
from app.db.database import Database
from app.db.repos.users import UserRecord
from app.settings import SettingsStore
from app.timeutil import to_sql, utcnow

log = logging.getLogger(__name__)

Scope = Literal["me", "partner", "both", "shared"]


class MemoryPolicyError(ValueError):
    """A memory operation that isn't allowed or can't be done; message is safe to show Claude."""


InboxKind = Literal["note", "category", "option"]


@dataclass(frozen=True)
class InboxItem:
    id: int
    owner: str
    target_path: str
    content: str
    reason: str | None
    source: str | None
    status: str
    created_at: str
    kind: str = "note"
    payload: dict[str, Any] = field(default_factory=dict)


def _inbox_row(r: sqlite3.Row) -> InboxItem:
    return InboxItem(
        r["id"],
        r["owner"],
        r["target_path"],
        r["content"],
        r["reason"],
        r["source"],
        r["status"],
        r["created_at"],
        r["kind"],
        json.loads(r["payload_json"]) if r["payload_json"] else {},
    )


# Applies an approved non-note suggestion (e.g. creates the category). Registered by the app so
# the memory layer doesn't depend on the decision engine.
Applier = Callable[[InboxItem], Awaitable[None]]


class MemoryService:
    def __init__(
        self,
        *,
        store: NoteStore,
        retriever: Retriever,
        db: Database,
        settings: SettingsStore,
        users: Sequence[UserRecord],
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.store = store
        self._retriever = retriever
        self._db = db
        self._settings = settings
        self._users = list(users)
        self._slugs = [u.slug for u in users]
        self._clock = clock
        self.appliers: dict[str, Applier] = {}

    # --- owners ------------------------------------------------------------------------------

    def owners_for(self, scope: Scope, asker: str) -> list[str]:
        """§6.5 step 1. 'partner' means everyone except the asker."""
        if scope == "me":
            return [asker, "shared"]
        if scope == "partner":
            return [*(s for s in self._slugs if s != asker), "shared"]
        if scope == "shared":
            return ["shared"]
        return [*self._slugs, "shared"]

    def readable_owners(self, asker: str, is_group: bool) -> list[str]:
        """read_note: the group sees everyone's notes; a DM sees the asker's and shared notes,
        plus everyone's pinned profiles (constraints are needed for 'both' decisions)."""
        return [*self._slugs, "shared"] if is_group else [asker, "shared"]

    # --- reading -----------------------------------------------------------------------------

    async def search(
        self, query: str, *, asker: str, scope: Scope, k: int | None = None
    ) -> list[Hit]:
        s = await self._settings.load()
        return await self._retriever.search(
            query, self.owners_for(scope, asker), k or s.memory_search_k
        )

    async def read(self, path: str, *, asker: str, is_group: bool) -> tuple[str, Note]:
        rel = nt.normalise_rel(path)
        note = await self.store.read(rel)
        if note is None:
            raise MemoryPolicyError(f"no note at {rel}")
        allowed = self.readable_owners(asker, is_group)
        if note.owner not in allowed and not (note.pinned and rel.startswith("people/")):
            raise MemoryPolicyError(f"{rel} belongs to {note.owner} and isn't readable here")
        return rel, note

    async def pinned_block(self) -> str | None:
        """[3] in §7.2: every pinned note (all users + shared). Allergies must never depend on
        retrieval recall (§6.6), and 'both' decisions need everyone's constraints."""
        s = await self._settings.load()
        pinned = await self.store.pinned_notes([*self._slugs, "shared"])
        if not pinned:
            return None
        parts = [
            "Pinned notes: always true. Respect these constraints for everyone a decision is for."
        ]
        for rel, note in pinned:
            section = f"### {rel} (owner: {note.owner})\n{note.body.strip()}"
            if note.avoid_tags:
                section += f"\nAvoid tags (enforced in code): {', '.join(note.avoid_tags)}"
            parts.append(section)
        text = "\n\n".join(parts)
        if len(text) > s.memory_pinned_max_chars:
            log.warning("pinned notes truncated", extra={"chars": len(text)})
            text = text[: s.memory_pinned_max_chars] + "\n[…truncated]"
        return text

    async def avoid_tags(self, for_users: str) -> set[str]:
        slugs = self._slugs if for_users == "both" else [for_users]
        return await self.store.avoid_tags(slugs)

    # --- writing (explicit) ------------------------------------------------------------------

    async def write(
        self,
        path: str,
        *,
        mode: WriteMode,
        content: str,
        heading: str | None = None,
        title: str | None = None,
        tags: Sequence[str] = (),
        add_avoid_tags: Sequence[str] = (),
        remove_avoid_tags: Sequence[str] = (),
        source: str = "",
    ) -> WriteResult:
        try:
            rel = nt.normalise_rel(path)
        except PathError as e:
            raise MemoryPolicyError(str(e)) from e
        if not nt.writable_by_claude(rel, self._slugs):
            raise MemoryPolicyError(
                f"can't write {rel}. Allowed: people/<user>.md, memories/<user>/<name>.md, "
                "shared/household.md, shared/places/<name>.md, shared/topics/<name>.md"
            )
        if (add_avoid_tags or remove_avoid_tags) and not rel.startswith("people/"):
            raise MemoryPolicyError("avoid_tags can only be set on people/<user>.md")
        return await self.store.write(
            rel,
            mode=mode,
            content=content,
            heading=heading,
            title=title,
            tags=tags,
            add_avoid_tags=add_avoid_tags,
            remove_avoid_tags=remove_avoid_tags,
            source=source,
        )

    # --- inbox (implicit memories) -----------------------------------------------------------

    def target_for(self, owner: str, topic: str) -> str:
        slug = nt.slug_for_title(topic)
        rel = f"shared/topics/{slug}.md" if owner == "shared" else f"memories/{owner}/{slug}.md"
        return nt.normalise_rel(rel)

    async def propose(
        self,
        *,
        owner: str,
        content: str,
        reason: str,
        topic: str,
        source: str,
        force_review: bool = False,
    ) -> InboxItem:
        """Queue an implicit memory (§6.7). ``memory.auto_approve`` applies it straight away,
        unless ``force_review`` (web facts, §7.5)."""
        if owner != "shared" and owner not in self._slugs:
            raise MemoryPolicyError(f"owner must be one of {[*self._slugs, 'shared']}")
        target = self.target_for(owner, topic)
        now = to_sql(self._clock())

        def _ins(c: sqlite3.Connection) -> int:
            cur = c.execute(
                "INSERT INTO memory_inbox(owner, target_path, content, reason, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (owner, target, content.strip(), reason.strip(), source, now),
            )
            return int(cur.lastrowid or 0)

        item_id = await self._db.write(_ins)
        log.info("memory proposed", extra={"inbox_id": item_id, "owner": owner})
        s = await self._settings.load()
        if s.memory_auto_approve and not force_review:
            return await self.decide(item_id, approve=True, user_id=None)
        item = await self.get(item_id)
        assert item is not None
        return item

    async def suggest(
        self, *, kind: InboxKind, content: str, reason: str, source: str, payload: dict[str, Any]
    ) -> InboxItem:
        """Queue a non-note suggestion (§10.4 harvester): a category or option to review."""
        now = to_sql(self._clock())

        def _ins(c: sqlite3.Connection) -> int:
            cur = c.execute(
                "INSERT INTO memory_inbox(owner, target_path, content, reason, source, created_at, "
                "kind, payload_json) VALUES ('shared', '', ?, ?, ?, ?, ?, ?)",
                (
                    content.strip(),
                    reason.strip(),
                    source,
                    now,
                    kind,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            return int(cur.lastrowid or 0)

        item = await self.get(await self._db.write(_ins))
        assert item is not None
        return item

    async def get(self, item_id: int) -> InboxItem | None:
        r = await self._db.read(
            lambda c: c.execute("SELECT * FROM memory_inbox WHERE id = ?", (item_id,)).fetchone()
        )
        return _inbox_row(r) if r else None

    async def pending(self, limit: int = 10) -> list[InboxItem]:
        rows = await self._db.read(
            lambda c: c.execute(
                "SELECT * FROM memory_inbox WHERE status = 'pending' ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        )
        return [_inbox_row(r) for r in rows]

    async def pending_count(self) -> int:
        row = await self._db.read(
            lambda c: c.execute(
                "SELECT COUNT(*) FROM memory_inbox WHERE status = 'pending'"
            ).fetchone()
        )
        return int(row[0])

    async def decide(self, item_id: int, *, approve: bool, user_id: int | None) -> InboxItem:
        """Approve (append to the target note) or reject. Only a pending item can change."""
        now = to_sql(self._clock())
        status = "approved" if approve else "rejected"
        changed = await self._db.write(
            lambda c: (
                c.execute(
                    "UPDATE memory_inbox SET status = ?, decided_at = ?, decided_by = ? "
                    "WHERE id = ? AND status = 'pending'",
                    (status, now, user_id, item_id),
                ).rowcount
            )
        )
        item = await self.get(item_id)
        if item is None:
            raise MemoryPolicyError(f"no inbox item {item_id}")
        if changed and approve and item.kind != "note":
            applier = self.appliers.get(item.kind)
            if applier is None:
                log.warning("no applier for inbox kind", extra={"kind": item.kind})
            else:
                await applier(item)
        elif changed and approve:
            exists = await self.store.exists(item.target_path)
            topic = item.target_path.rsplit("/", 1)[-1][:-3].replace("-", " ")
            await self.store.write(
                item.target_path,
                mode="append" if exists else "create",
                content=f"- {item.content}",
                title=topic[:1].upper() + topic[1:],
                source=item.source or f"inbox:{item.id}",
            )
        return item

    # --- decision log (§8.4) -----------------------------------------------------------------

    async def log_decision(self, line: str) -> None:
        try:
            await self.store.append_log(line)
        except Exception:
            log.exception("decision log mirror failed")
