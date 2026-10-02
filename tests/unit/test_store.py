"""NoteStore (§6.3): atomic write, synchronous reindex, reconcile, path safety."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from app.brain import index
from app.brain.notes import PathError
from app.brain.store import NoteError, NoteStore
from tests.conftest import TZ, Env
from tests.fakes.fake_embedder import FakeEmbedder


def _store(env: Env, embedder: FakeEmbedder | None = None) -> tuple[NoteStore, FakeEmbedder]:
    emb = embedder or FakeEmbedder()
    return NoteStore(root=env.vault, db=env.db, embedder=emb, tz=TZ), emb


async def _counts(env: Env) -> dict[str, int]:
    return await env.db.read(index.counts)


async def _fts(env: Env, word: str) -> list[int]:
    rows = await env.db.read(
        lambda c: c.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?", (word,)
        ).fetchall()
    )
    return [r[0] for r in rows]


async def test_create_writes_file_and_index(env: Env) -> None:
    store, _ = _store(env)
    r = await store.write(
        "shared/topics/sichuan",
        mode="create",
        title="Sichuan",
        content="Mala is numbing. See [[shared/places/sichuan-kitchen]].",
        tags=["food"],
    )
    assert (r.path, r.created) == ("shared/topics/sichuan.md", True)
    text = (env.vault / r.path).read_text()
    assert text.startswith("---\nid: ") and "# Sichuan" in text and "owner: shared" in text
    assert not list(env.vault.rglob(".*.tmp"))
    assert await _counts(env) == {"notes": 1, "chunks": 1, "chunks_vec": 1, "links": 1}
    assert await _fts(env, "numbing")
    note = await env.db.read(lambda c: c.execute("SELECT * FROM notes").fetchone())
    assert (note["owner"], note["type"], note["title"]) == ("shared", "fact", "Sichuan")


async def test_modes_and_errors(env: Env) -> None:
    store, _ = _store(env)
    with pytest.raises(NoteError):
        await store.write("memories/jack/x", mode="replace", content="x")
    await store.write("memories/jack/x", mode="create", content="## A\n\none")
    with pytest.raises(NoteError):
        await store.write("memories/jack/x", mode="create", content="again")
    with pytest.raises(NoteError):
        await store.write("memories/jack/x", mode="replace_section", content="x")
    await store.write("memories/jack/x", mode="append", heading="A", content="two")
    await store.write("memories/jack/x", mode="replace_section", heading="A", content="three")
    note = await store.read("memories/jack/x")
    assert note is not None and "three" in note.body and "one" not in note.body
    await store.write("memories/jack/x", mode="replace", content="only this")
    note = await store.read("memories/jack/x")
    assert note is not None and note.body.startswith("# X\n") and "three" not in note.body
    assert note.meta["created"] != "" and note.meta["updated"] >= note.meta["created"]


async def test_reindex_reuses_unchanged_chunks_and_cleans_fts(env: Env) -> None:
    store, emb = _store(env)
    body = (
        "## Food\n\n"
        + "Likes laksa a lot. " * 20
        + "\n\n## Drinks\n\n"
        + "Kopi-o kosong daily. " * 20
    )
    await store.write("people/jack", mode="create", content=body)
    first = sum(len(c) for c in emb.passage_calls)
    await store.write(
        "people/jack", mode="replace_section", heading="Drinks", content="Teh peng only. " * 20
    )
    assert sum(len(c) for c in emb.passage_calls) - first == 1  # only the changed chunk
    assert await _fts(env, "teh") and not await _fts(env, "kopi")  # old text gone from FTS
    c = await _counts(env)
    assert c["chunks"] == c["chunks_vec"] == 2


async def test_atomic_write_leaves_old_file_on_failure(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = _store(env)
    await store.write("memories/jack/x", mode="create", content="original")
    before = (env.vault / "memories/jack/x.md").read_text()

    def boom(*a: Any, **k: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        await store.write("memories/jack/x", mode="append", content="never lands")
    assert (env.vault / "memories/jack/x.md").read_text() == before


@pytest.mark.parametrize("bad", ["../escape", "/abs/path", "memories/../../x", "shared/.secret"])
async def test_traversal_rejected(env: Env, bad: str) -> None:
    store, _ = _store(env)
    with pytest.raises(PathError):
        await store.write(bad, mode="create", content="x")
    assert not (env.vault.parent / "escape.md").exists()


async def test_symlinked_dir_rejected(env: Env, tmp_path: Path) -> None:
    store, _ = _store(env)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (env.vault / "shared").mkdir(parents=True)
    (env.vault / "shared" / "places").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathError):
        await store.write("shared/places/x", mode="create", content="x")
    assert list(outside.iterdir()) == []


async def test_logs_are_written_but_not_indexed(env: Env) -> None:
    store, _ = _store(env)
    rel = await store.append_log("- 19:00 · Dinner · **Pho**")
    await store.append_log("- 20:00 · Movie · **Arrival**")
    text = (env.vault / rel).read_text()
    assert rel.startswith("logs/") and "Pho" in text and "Arrival" in text
    assert (await _counts(env))["notes"] == 0


async def test_embedding_failure_marks_pending_then_reconcile_retries(env: Env) -> None:
    store, emb = _store(env)
    emb.fail = True
    await store.write("memories/jack/x", mode="create", content="laksa lover")
    rows = await env.db.read(lambda c: c.execute("SELECT embed_model FROM chunks").fetchall())
    assert [r[0] for r in rows] == [index.PENDING]
    assert await _fts(env, "laksa")  # still searchable by keyword
    emb.fail = False
    stats = await store.reconcile()
    assert stats["reindexed"] == 1
    rows = await env.db.read(lambda c: c.execute("SELECT embed_model FROM chunks").fetchall())
    assert [r[0] for r in rows] == [emb.model_id]
    assert (await _counts(env))["chunks_vec"] == 1


async def test_reconcile_detects_edits_new_and_deleted_files(env: Env) -> None:
    store, _ = _store(env)
    await store.write("memories/jack/a", mode="create", content="alpha")
    await store.write("memories/jack/b", mode="create", content="bravo")
    assert (await store.reconcile())["reindexed"] == 0  # nothing changed
    (env.vault / "memories/jack/a.md").write_text("---\nowner: jack\n---\n# A\n\ncharlie\n")
    (env.vault / "memories/jack/b.md").unlink()
    (env.vault / "shared/topics").mkdir(parents=True)
    (env.vault / "shared/topics/new.md").write_text("# New\n\ndelta\n")
    (env.vault / "shared/topics/.draft.md.tmp").write_text("ignored")
    stats = await store.reconcile()
    assert (stats["reindexed"], stats["removed"]) == (2, 1)
    assert await _fts(env, "charlie") and not await _fts(env, "bravo") and await _fts(env, "delta")


async def test_model_change_triggers_full_reindex(env: Env) -> None:
    store, _ = _store(env)
    await store.write("memories/jack/a", mode="create", content="alpha")
    await store.write("memories/jack/b", mode="create", content="bravo")
    new_store, _ = _store(env, FakeEmbedder("fake-embedder@other"))
    assert (await new_store.reconcile())["reindexed"] == 2
    rows = await env.db.read(
        lambda c: c.execute("SELECT DISTINCT embed_model FROM chunks").fetchall()
    )
    assert [r[0] for r in rows] == ["fake-embedder@other"]


async def test_rebuild_and_delete(env: Env) -> None:
    store, _ = _store(env)
    await store.write("memories/jack/a", mode="create", content="alpha [[memories/jack/b]]")
    await store.rebuild()
    assert (await _counts(env))["notes"] == 1
    assert await store.delete("memories/jack/a")
    assert not (env.vault / "memories/jack/a.md").exists()
    assert await _counts(env) == {"notes": 0, "chunks": 0, "chunks_vec": 0, "links": 0}
    assert not await _fts(env, "alpha")


async def test_skeleton_and_avoid_tags(env: Env) -> None:
    store, _ = _store(env)
    created = await store.ensure_skeleton(env.users)
    assert created == ["people/jack.md", "people/partner.md", "shared/household.md"]
    assert await store.ensure_skeleton(env.users) == []
    await store.write(
        "people/jack",
        mode="append",
        content="Hates coriander.",
        add_avoid_tags=["Contains:Coriander", "contains:peanut"],
    )
    await store.write("people/partner", mode="append", content="x", add_avoid_tags=["raw-fish"])
    assert await store.avoid_tags(["jack", "partner"]) == {
        "contains:coriander",
        "contains:peanut",
        "raw-fish",
    }
    await store.write(
        "people/jack",
        mode="append",
        content="peanuts ok now",
        remove_avoid_tags=["contains:peanut"],
    )
    assert await store.avoid_tags(["jack"]) == {"contains:coriander"}
    pinned = await store.pinned_notes(["jack", "partner", "shared"])
    assert [p for p, _ in pinned] == ["people/jack.md", "people/partner.md", "shared/household.md"]
