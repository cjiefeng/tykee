"""Pets (§10.6 step 3): structured ``pets:`` frontmatter in ``shared/household.md``.

```yaml
pets:
  - name: Mochi
    species: dog
    size: small        # small | medium | large
```

Claude sees them in the dynamic context and adds ``pet_friendly`` to ``must`` when a message
mentions one by name. Edited in the dashboard (Memory → Places).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.brain import notes as nt
from app.brain.store import NoteStore

HOUSEHOLD = "shared/household.md"
SIZES = ("small", "medium", "large")
EMOJI = {"dog": "🐶", "cat": "🐱"}


@dataclass(frozen=True)
class Pet:
    name: str
    species: str
    size: str | None = None

    def describe(self) -> str:
        return f"{self.name} ({' '.join(x for x in (self.size, self.species) if x)})"


def parse(meta: dict[str, Any]) -> list[Pet]:
    out: list[Pet] = []
    for raw in meta.get("pets") or []:
        if not isinstance(raw, dict) or not str(raw.get("name") or "").strip():
            continue
        size = str(raw.get("size") or "").strip().casefold() or None
        out.append(
            Pet(
                str(raw["name"]).strip(),
                str(raw.get("species") or "pet").strip().casefold(),
                size if size in SIZES else None,
            )
        )
    return out


def parse_lines(text: str) -> list[Pet]:
    """Dashboard form: 'Mochi dog small' per line (size optional)."""
    out: list[Pet] = []
    for line in text.splitlines():
        words = line.split()
        if not words:
            continue
        if len(words) < 2:
            raise ValueError(f"pet {line.strip()!r}: use 'name species [size]' (Mochi dog small)")
        size = words[2].casefold() if len(words) > 2 else None
        if size is not None and size not in SIZES:
            raise ValueError(f"pet {words[0]}: size is one of {', '.join(SIZES)}")
        out.append(Pet(words[0], words[1].casefold(), size))
    return out


def emoji(pets: Sequence[Pet]) -> str:
    kinds = {p.species for p in pets}
    return next((EMOJI[k] for k in ("dog", "cat") if k in kinds), "🐾")


async def load(store: NoteStore) -> list[Pet]:
    note = await store.read(HOUSEHOLD)
    return parse(note.meta) if note is not None else []


async def save(store: NoteStore, pets: Sequence[Pet]) -> None:
    """Rewrite the ``pets`` frontmatter, keeping the note's body and other fields."""
    note = await store.read(HOUSEHOLD)
    if note is None:
        note = nt.Note(meta={"pinned": True}, body="# Household\n")
    note.meta["pets"] = [
        {k: v for k, v in (("name", p.name), ("species", p.species), ("size", p.size)) if v}
        for p in pets
    ]
    if not pets:
        note.meta.pop("pets")
    await store.write_raw(HOUSEHOLD, nt.render(note))
