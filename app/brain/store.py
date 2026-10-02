"""NoteStore (§6.3): the only way anything writes to the vault.

write → validate path → render → tmp + fsync + rename (atomic) → chunk → embed only changed
chunks (embedder thread) → one SQLite transaction replacing the note's index rows. No file
watcher: startup ``reconcile()`` catches drift, retries pending embeddings and reindexes
everything when the embedding model/precision changes.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from app.brain import index
from app.brain import notes as nt
from app.brain.embedder import Embedder
from app.brain.index import IndexedChunk, NoteRow
from app.brain.notes import Note, PathError
from app.db.database import Database
from app.db.repos.users import UserRecord
from app.timeutil import to_sql, utcnow

log = logging.getLogger(__name__)

WriteMode = Literal["create", "append", "replace_section", "replace"]


class NoteError(ValueError):
    """A write that can't be done as asked (missing note, existing note on create, ...)."""


@dataclass(frozen=True)
class WriteResult:
    path: str
    created: bool


class NoteStore:
    def __init__(
        self,
        *,
        root: Path,
        db: Database,
        embedder: Embedder,
        tz: ZoneInfo,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.root = root
        self._db = db
        self._embedder = embedder
        self._tz = tz
        self._clock = clock
        self._lock = asyncio.Lock()  # one writer at a time keeps file + index in step

    # --- file I/O (runs in a worker thread) --------------------------------------------------

    def _abs(self, rel: str) -> Path:
        return nt.resolve_in_vault(self.root, rel)

    def _atomic_write(self, rel: str, text: str) -> bytes:
        path = self._abs(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        tmp = path.with_name(f".{path.name}.tmp")
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        nt.fsync_dir(path.parent)
        return data

    def _read_bytes(self, rel: str) -> bytes | None:
        path = self._abs(rel)
        return path.read_bytes() if path.is_file() else None

    async def _io[T](self, fn: Callable[..., T], *args: object) -> T:
        return await asyncio.to_thread(fn, *args)

    # --- reading -----------------------------------------------------------------------------

    async def read(self, path: str) -> Note | None:
        rel = nt.normalise_rel(path)
        data = await self._io(self._read_bytes, rel)
        return nt.parse(data.decode("utf-8")) if data is not None else None

    async def exists(self, path: str) -> bool:
        return await self.read(path) is not None

    # --- writing -----------------------------------------------------------------------------

    def _now_iso(self) -> str:
        return self._clock().astimezone(self._tz).isoformat(timespec="seconds")

    async def write(
        self,
        path: str,
        *,
        mode: WriteMode,
        content: str,
        heading: str | None = None,
        title: str | None = None,
        note_type: str | None = None,
        tags: Sequence[str] = (),
        source: str = "",
        add_avoid_tags: Sequence[str] = (),
        remove_avoid_tags: Sequence[str] = (),
        pinned: bool | None = None,
    ) -> WriteResult:
        rel = nt.normalise_rel(path)
        async with self._lock:
            existing = await self.read(rel)
            now = self._now_iso()
            if existing is None:
                if mode not in ("create", "append"):
                    raise NoteError(f"{rel} doesn't exist; use mode 'create'")
                heading_title = title or nt.title_for(rel)
                body = content.strip()
                if not body.startswith("# "):
                    body = f"# {heading_title}\n\n{body}"
                note = Note(
                    meta={
                        "id": nt.new_ulid(),
                        "owner": nt.owner_for(rel),
                        "type": note_type or nt.default_type_for(rel),
                        "tags": list(tags),
                        "pinned": bool(pinned),
                        "source": source,
                        "created": now,
                        "updated": now,
                    },
                    body=body + "\n",
                )
                created = True
            else:
                if mode == "create":
                    raise NoteError(f"{rel} already exists; use append or replace_section")
                note = existing
                if mode == "append":
                    note.body = nt.append(note.body, content, heading)
                elif mode == "replace_section":
                    if not heading:
                        raise NoteError("replace_section needs a heading")
                    note.body = nt.replace_section(note.body, heading, content)
                else:  # replace: whole body, keeping the title line
                    body = content.strip()
                    if not body.startswith("# ") and note.title:
                        body = f"# {note.title}\n\n{body}"
                    note.body = body + "\n"
                if tags:
                    note.meta["tags"] = sorted({*map(str, note.meta.get("tags") or []), *tags})
                if pinned is not None:
                    note.meta["pinned"] = pinned
                note.meta["updated"] = now
                created = False
            if add_avoid_tags or remove_avoid_tags:
                current = set(note.avoid_tags)
                current |= {t.strip().casefold() for t in add_avoid_tags if t.strip()}
                current -= {t.strip().casefold() for t in remove_avoid_tags}
                note.meta["avoid_tags"] = sorted(current)
            data = await self._io(self._atomic_write, rel, nt.render(note))
            await self._reindex(rel, data)
        log.info("note written", extra={"path": rel, "mode": mode, "new_note": created})
        return WriteResult(rel, created)

    async def write_raw(self, path: str, text: str) -> str:
        """Dashboard edit (§11): replace the whole file, frontmatter included. The text must
        parse; the note keeps its id and gets a fresh ``updated`` stamp."""
        rel = nt.normalise_rel(path)
        try:
            note = nt.parse(text)
        except Exception as e:
            raise NoteError(f"can't parse note: {e}") from e
        async with self._lock:
            existing = await self.read(rel)
            if existing is not None and "id" in existing.meta:
                note.meta.setdefault("id", existing.meta["id"])
            note.meta.setdefault("id", nt.new_ulid())
            note.meta.setdefault("owner", nt.owner_for(rel))
            note.meta.setdefault("type", nt.default_type_for(rel))
            note.meta["updated"] = self._now_iso()
            data = await self._io(self._atomic_write, rel, nt.render(note))
            await self._reindex(rel, data)
        log.info("note edited in dashboard", extra={"path": rel})
        return rel

    async def set_pinned(self, path: str, pinned: bool) -> None:
        rel = nt.normalise_rel(path)
        note = await self.read(rel)
        if note is None:
            raise NoteError(f"{rel} doesn't exist")
        note.meta["pinned"] = pinned
        await self.write_raw(rel, nt.render(note))

    async def delete(self, path: str) -> bool:
        rel = nt.normalise_rel(path)
        async with self._lock:
            abs_path = self._abs(rel)
            existed = await self._io(abs_path.is_file)
            if existed:
                await self._io(abs_path.unlink)
            await self._db.write(lambda c: index.delete_note(c, rel))
        return bool(existed)

    async def append_log(self, line: str) -> str:
        """Human-readable decision mirror (§8.4): logs/YYYY/MM/YYYY-MM-DD.md, never indexed."""
        local = self._clock().astimezone(self._tz)
        rel = f"logs/{local:%Y}/{local:%m}/{local:%Y-%m-%d}.md"
        exists = await self.exists(rel)
        await self.write(
            rel,
            mode="append" if exists else "create",
            content=line,
            title=f"Decisions {local:%Y-%m-%d}",
            note_type="log",
        )
        return rel

    # --- indexing ----------------------------------------------------------------------------

    async def _reindex(self, rel: str, data: bytes) -> None:
        if not nt.is_indexed(rel):
            return
        note = nt.parse(data.decode("utf-8"))
        title = note.title or rel.rsplit("/", 1)[-1][:-3]
        chunks = nt.chunk_note(note, title)
        model = self._embedder.model_id
        reuse = await self._db.read(lambda c: index.reusable_vectors(c, rel, model))
        todo = [ch for ch in chunks if ch.hash not in reuse]
        fresh: dict[str, bytes] = {}
        embed_model = model
        if todo:
            try:
                vectors = await self._embedder.embed_passages([ch.text for ch in todo])
                fresh = {ch.hash: index.pack(v) for ch, v in zip(todo, vectors, strict=True)}
            except Exception:
                # §14.4: the file is already written; index without vectors and retry later.
                log.exception("embedding failed; chunks marked pending", extra={"path": rel})
                embed_model = index.PENDING
        indexed = [
            IndexedChunk(
                ch,
                reuse.get(ch.hash) or fresh.get(ch.hash),
                model if (ch.hash in reuse or ch.hash in fresh) else embed_model,
            )
            for ch in chunks
        ]
        row = NoteRow(
            path=rel,
            owner=note.owner if note.owner else nt.owner_for(rel),
            type=note.type,
            title=title,
            file_hash=nt.file_hash(data),
            pinned=note.pinned,
            updated_at=to_sql(self._clock()),
        )
        links = nt.wikilinks(note.body)
        await self._db.write(lambda c: index.replace_note(c, row, indexed, links))

    def _walk(self) -> dict[str, bytes]:
        found: dict[str, bytes] = {}
        if not self.root.exists():
            return found
        root = self.root.resolve()
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if not name.endswith(".md") or name.startswith("."):
                    continue
                full = Path(dirpath) / name
                rel = full.relative_to(root).as_posix()
                try:
                    rel = nt.normalise_rel(rel)
                except PathError:
                    log.warning("skipping unexpected vault file", extra={"path": rel})
                    continue
                if nt.is_indexed(rel) and not full.is_symlink():
                    found[rel] = full.read_bytes()
        return found

    async def reconcile(self) -> dict[str, int]:
        """Startup: index new/changed files, drop rows for deleted ones, re-embed pending or
        stale-model chunks."""
        async with self._lock:
            files = await self._io(self._walk)
            known = await self._db.read(index.file_hashes)
            model = self._embedder.model_id
            stale = set(await self._db.read(lambda c: index.stale_paths(c, model)))
            changed = [
                rel for rel, data in files.items()
                if known.get(rel) != nt.file_hash(data) or rel in stale
            ]  # fmt: skip
            removed = [rel for rel in known if rel not in files]
            for rel in removed:
                await self._db.write(functools.partial(index.delete_note, path=rel))
            for rel in sorted(changed):
                await self._reindex(rel, files[rel])
        stats = {"files": len(files), "reindexed": len(changed), "removed": len(removed)}
        log.info("vault reconciled", extra=stats)
        return stats

    async def retry_pending(self) -> dict[str, int] | None:
        """§14.4: re-embed chunks an embedding failure left 'pending' (scheduled hourly)."""
        pending = await self._db.read(
            lambda c: c.execute(
                "SELECT COUNT(*) FROM chunks WHERE embed_model = ?", (index.PENDING,)
            ).fetchone()[0]
        )
        return await self.reconcile() if pending else None

    async def rebuild(self) -> dict[str, int]:
        """System → Reindex (§14.4): drop the whole derived index and rebuild from files."""
        await self._db.write(index.drop_all)
        return await self.reconcile()

    # --- skeleton & constraints --------------------------------------------------------------

    async def ensure_skeleton(self, users: Sequence[UserRecord]) -> list[str]:
        """Pinned profile notes for each user plus the shared household note (§6.1)."""
        created: list[str] = []
        for u in users:
            rel = f"people/{u.slug}.md"
            if not await self.exists(rel):
                await self.write(
                    rel,
                    mode="create",
                    title=u.display_name,
                    content="## Constraints\n\n(none recorded yet)\n\n## Preferences\n\n(none yet)",
                    pinned=True,
                    source="bootstrap",
                )
                created.append(rel)
        if not await self.exists("shared/household.md"):
            await self.write(
                "shared/household.md",
                mode="create",
                title="Household",
                content="Shared constraints, budget norms, kitchen equipment.",
                pinned=True,
                source="bootstrap",
            )
            created.append("shared/household.md")
        return created

    async def avoid_tags(self, slugs: Sequence[str]) -> set[str]:
        """Hard constraints (§8.2): union of ``avoid_tags`` on people/<slug>.md."""
        out: set[str] = set()
        for slug in slugs:
            note = await self.read(f"people/{slug}.md")
            if note is not None:
                out |= set(note.avoid_tags)
        return out

    async def pinned_notes(self, owners: Sequence[str]) -> list[tuple[str, Note]]:
        rows = await self._db.read(
            lambda c: c.execute(
                f"SELECT path FROM notes WHERE pinned = 1 AND owner IN "
                f"({','.join('?' * len(owners))}) ORDER BY path",
                tuple(owners),
            ).fetchall()
        )
        out: list[tuple[str, Note]] = []
        for r in rows:
            note = await self.read(r["path"])
            if note is not None:
                out.append((r["path"], note))
        return out
