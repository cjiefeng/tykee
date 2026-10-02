"""Note files (§6.1-6.4): paths, frontmatter, editing, chunking, links."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.brain import notes as nt
from app.brain.notes import Note, PathError

SLUGS = ["jack", "partner"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("memories/jack/food", "memories/jack/food.md"),
        ("shared/places/ah-hock_laksa.md", "shared/places/ah-hock_laksa.md"),
        ("shared\\topics\\movies.md", "shared/topics/movies.md"),
        ("memories/partner/甜品.md", "memories/partner/甜品.md"),
    ],
)
def test_normalise_rel(raw: str, expected: str) -> None:
    assert nt.normalise_rel(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "/etc/passwd",
        "../outside.md",
        "memories/../../x.md",
        "a/./b.md",
        "a//b.md",
        "memories/jack/.hidden.md",
        "memories/jack/semi;colon.md",
        "x\x00.md",
        "people/-x.md",
    ],
)
def test_normalise_rejects(raw: str) -> None:
    with pytest.raises(PathError):
        nt.normalise_rel(raw)


def test_resolve_rejects_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    (root / "shared").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "shared" / "places").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathError):
        nt.resolve_in_vault(root, "shared/places/x.md")
    assert (
        nt.resolve_in_vault(root, "shared/topics/x.md") == (root / "shared/topics/x.md").resolve()
    )


@pytest.mark.parametrize(
    ("rel", "ok"),
    [
        ("people/jack.md", True),
        ("people/bob.md", False),
        ("people/jack/x.md", False),
        ("memories/partner/food.md", True),
        ("memories/bob/food.md", False),
        ("memories/jack.md", False),
        ("shared/household.md", True),
        ("shared/other.md", False),
        ("shared/places/x.md", True),
        ("shared/topics/x.md", True),
        ("shared/misc/x.md", False),
        ("logs/2026/10/2026-10-02.md", False),
        ("notes/x.md", False),
    ],
)
def test_writable_by_claude(rel: str, ok: bool) -> None:
    assert nt.writable_by_claude(rel, SLUGS) is ok


def test_owner_and_type_defaults() -> None:
    assert (nt.owner_for("people/jack.md"), nt.default_type_for("people/jack.md")) == (
        "jack",
        "profile",
    )
    assert (
        nt.owner_for("memories/partner/x.md"),
        nt.default_type_for("memories/partner/x.md"),
    ) == ("partner", "fact")
    assert (nt.owner_for("shared/places/x.md"), nt.default_type_for("shared/places/x.md")) == (
        "shared",
        "place",
    )
    assert not nt.is_indexed("logs/2026/10/x.md") and nt.is_indexed("shared/topics/x.md")


def test_parse_render_roundtrip_keeps_unicode() -> None:
    note = Note(
        meta={
            "id": "01J",
            "owner": "partner",
            "type": "fact",
            "tags": ["甜品"],
            "pinned": False,
            "avoid_tags": ["Contains:Peanut", " "],
        },
        body="# 甜品\n\n她喜欢榴莲。\n",
    )
    text = nt.render(note)
    assert "甜品" in text and "\\u" not in text
    back = nt.parse(text)
    assert back.meta["tags"] == ["甜品"] and back.title == "甜品" and back.owner == "partner"
    assert back.avoid_tags == ["contains:peanut"]


def test_append_and_replace_section() -> None:
    body = "# Jack\n\n## Constraints\n\nNo peanuts.\n\n## Preferences\n\nLikes laksa.\n"
    b = nt.append(body, "Hates coriander.", "Constraints")
    assert b.index("Hates coriander.") < b.index("## Preferences")
    assert (
        nt.append(body, "Bike to work.", "Routines")
        .rstrip()
        .endswith("## Routines\n\nBike to work.")
    )
    assert nt.append(body, "Tail line.").rstrip().endswith("Tail line.")
    r = nt.replace_section(b, "constraints", "Peanuts are fine now.")
    assert "No peanuts" not in r and "Hates coriander" not in r and "Likes laksa." in r
    assert r.index("Peanuts are fine now.") < r.index("## Preferences")


def test_chunking_headings_prefix_and_merge() -> None:
    long_para = " ".join(["word"] * 400)  # ~2000 chars → split
    body = (
        "# Jack\n\nIntro line.\n\n## Food\n\n"
        + "Likes laksa. " * 30
        + "\n\n### Dislikes\n\nCoriander.\n\n"
        "## Long\n\n" + long_para + "\n"
    )
    chunks = nt.chunk_note(Note({}, body), "Jack")
    assert all(c.text.split("\n", 1)[0].startswith("Jack") for c in chunks)
    assert any(c.heading == "Food" for c in chunks)
    # tiny "Dislikes" section merged into the Food chunk, with its heading kept in the text
    food = next(c for c in chunks if c.heading == "Food")
    assert "Food > Dislikes:" in food.text and "Coriander." in food.text
    long_chunks = [c for c in chunks if c.heading == "Long"]
    assert len(long_chunks) >= 2 and all(
        len(c.text) <= nt.CHUNK_MAX_CHARS + 50 for c in long_chunks
    )
    assert [c.ord for c in chunks] == list(range(len(chunks)))


def test_single_short_note_is_one_chunk() -> None:
    chunks = nt.chunk_note(
        nt.parse("---\nowner: jack\n---\n# Coffee\n\nKopi-o kosong.\n"), "Coffee"
    )
    assert [c.text for c in chunks] == ["Coffee\nKopi-o kosong."]


def test_wikilinks() -> None:
    body = (
        "See [[shared/topics/sichuan]] and [[shared/places/x.md|the place]] "
        "and [[../evil]] [[shared/topics/sichuan#Mala]]."
    )
    assert nt.wikilinks(body) == ["shared/topics/sichuan.md", "shared/places/x.md"]


def test_ulid_shape_and_order() -> None:
    a, b = nt.new_ulid(1_000), nt.new_ulid(2_000)
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", a) and a[:10] < b[:10]
