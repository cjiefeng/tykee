"""Note files (§6.1-6.2, §6.4): frontmatter, path rules, chunking and wikilinks. Pure functions;
no I/O beyond what callers pass in."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

import frontmatter

NOTE_TYPES = ("profile", "preference", "place", "fact", "log")
LOGS_DIR = "logs"

# Allowed relative locations for notes the bot (or Claude) may write. `{user}` is a user slug.
_SEGMENT = re.compile(r"^[\w][\w-]*$")
_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")

CHUNK_MAX_CHARS = 1500  # ≈ 400-500 tokens of English; e5 truncates at 512 tokens anyway
CHUNK_MIN_CHARS = 200  # sections shorter than this merge into the previous chunk


class PathError(ValueError):
    """A note path that's malformed, outside the vault, or not allowed for this operation."""


# --- ULIDs -----------------------------------------------------------------------------------

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid(now_ms: int | None = None) -> str:
    ts = now_ms if now_ms is not None else int(time.time() * 1000)
    value = (ts << 80) | secrets.randbits(80)
    return "".join(_CROCKFORD[(value >> (5 * i)) & 31] for i in reversed(range(26)))


# --- paths -----------------------------------------------------------------------------------


def normalise_rel(path: str) -> str:
    """Canonical vault-relative path: posix separators, '.md' suffix, validated segments."""
    raw = path.strip().replace("\\", "/")
    if not raw or raw.startswith("/") or "\x00" in raw:
        raise PathError(f"invalid note path {path!r}")
    if not raw.endswith(".md"):
        raw += ".md"
    parts = tuple(raw.split("/"))  # not PurePosixPath: it silently drops '.' and '//'
    if any(p in ("", ".", "..") for p in parts):
        raise PathError(f"invalid note path {path!r}")
    *dirs, filename = parts
    stem = filename[: -len(".md")]
    for seg in [*dirs, stem]:
        if not _SEGMENT.match(seg):
            raise PathError(f"invalid path segment {seg!r} in {path!r}")
    return "/".join(parts)


def resolve_in_vault(root: Path, rel: str) -> Path:
    """Absolute path for ``rel`` that is guaranteed to be inside ``root`` with no symlinks on
    the way (§12: path traversal)."""
    root_resolved = root.resolve()
    target = root_resolved.joinpath(*PurePosixPath(rel).parts)
    current = root_resolved
    for part in PurePosixPath(rel).parts:
        current = current / part
        if current.is_symlink():
            raise PathError(f"symlinks are not allowed in the vault: {rel!r}")
    if root_resolved not in target.resolve().parents:
        raise PathError(f"path escapes the vault: {rel!r}")
    return target


def writable_by_claude(rel: str, user_slugs: Sequence[str]) -> bool:
    """Where ``write_note`` may write (§6.1, §12). logs/ is internal only."""
    parts = PurePosixPath(rel).parts
    if parts[0] == "people":
        return len(parts) == 2 and parts[1][:-3] in user_slugs
    if parts[0] == "memories":
        return len(parts) == 3 and parts[1] in user_slugs
    if parts[0] == "shared":
        if len(parts) == 2:
            return parts[1] == "household.md"
        return len(parts) == 3 and parts[1] in ("places", "topics")
    return False


def owner_for(rel: str) -> str:
    parts = PurePosixPath(rel).parts
    if parts[0] == "people" and len(parts) == 2:
        return parts[1][:-3]
    if parts[0] == "memories" and len(parts) >= 3:
        return parts[1]
    return "shared"


def default_type_for(rel: str) -> str:
    parts = PurePosixPath(rel).parts
    if parts[0] == "people":
        return "profile"
    if parts[0] == LOGS_DIR:
        return "log"
    if parts[:2] == ("shared", "places"):
        return "place"
    if parts == ("shared", "household.md"):
        return "profile"
    return "fact"


def is_indexed(rel: str) -> bool:
    """logs/ is written through NoteStore but never indexed (§6.3)."""
    return PurePosixPath(rel).parts[0] != LOGS_DIR


def title_for(rel: str) -> str:
    """Readable default title from the filename: 'ah-hock-laksa.md' → 'Ah hock laksa'."""
    words = rel.rsplit("/", 1)[-1][: -len(".md")].replace("-", " ").replace("_", " ").strip()
    return words[:1].upper() + words[1:]


def slug_for_title(title: str) -> str:
    s = re.sub(r"[^\w]+", "-", title.casefold()).strip("-")
    return s[:60].strip("-") or "note"


# --- note model ------------------------------------------------------------------------------


@dataclass
class Note:
    meta: dict[str, Any]
    body: str

    @property
    def title(self) -> str:
        for line in self.body.splitlines():
            m = _HEADING.match(line)
            if m and len(m.group(1)) == 1:
                return m.group(2)
        return str(self.meta.get("title") or "")

    @property
    def pinned(self) -> bool:
        return bool(self.meta.get("pinned", False))

    @property
    def owner(self) -> str:
        return str(self.meta.get("owner", "shared"))

    @property
    def type(self) -> str:
        t = str(self.meta.get("type", "fact"))
        return t if t in NOTE_TYPES else "fact"

    @property
    def avoid_tags(self) -> list[str]:
        raw = self.meta.get("avoid_tags") or []
        return [str(t).strip().casefold() for t in raw if str(t).strip()]


def parse(text: str) -> Note:
    post = frontmatter.loads(text)
    return Note(meta=dict(post.metadata), body=post.content.strip() + "\n")


def _yaml_safe(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds")
    return v


def render(note: Note) -> str:
    meta = {k: _yaml_safe(v) for k, v in note.meta.items()}
    post = frontmatter.Post(note.body.strip() + "\n", **meta)
    return frontmatter.dumps(post, sort_keys=False) + "\n"


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- body editing (write_note modes) ---------------------------------------------------------


def _section_bounds(lines: list[str], heading: str) -> tuple[int, int, int] | None:
    """(start, end, level) of the section titled ``heading`` (case-insensitive), where ``start``
    is the heading line and ``end`` the first line of the next heading at the same or higher
    level."""
    want = heading.strip().lstrip("#").strip().casefold()
    for i, line in enumerate(lines):
        m = _HEADING.match(line)
        if m and m.group(2).casefold() == want:
            level = len(m.group(1))
            for j in range(i + 1, len(lines)):
                n = _HEADING.match(lines[j])
                if n and len(n.group(1)) <= level:
                    return i, j, level
            return i, len(lines), level
    return None


def append(body: str, content: str, heading: str | None = None) -> str:
    """Append ``content`` at the end of the note, or at the end of section ``heading``
    (created as an H2 if missing)."""
    content = content.strip()
    lines = body.rstrip("\n").split("\n")
    bounds = _section_bounds(lines, heading) if heading else None
    if bounds is None:
        if heading:
            content = f"## {heading.strip().lstrip('#').strip()}\n\n{content}"
        return "\n".join([*lines, "", content]).strip() + "\n"
    _, end, _ = bounds
    head = lines[:end]
    while head and not head[-1].strip():
        head.pop()
    tail = lines[end:]
    out = [*head, "", content, *(["", *tail] if tail else [])]
    return "\n".join(out).strip() + "\n"


def replace_section(body: str, heading: str, content: str) -> str:
    lines = body.rstrip("\n").split("\n")
    bounds = _section_bounds(lines, heading)
    if bounds is None:
        return append(body, content, heading)
    start, end, _ = bounds
    new = [*lines[: start + 1], "", content.strip(), ""]
    rest = lines[end:]
    return "\n".join([*new, *rest]).rstrip() + "\n"


# --- chunking & links ------------------------------------------------------------------------


@dataclass(frozen=True)
class Chunk:
    ord: int
    heading: str | None
    text: str

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()


@dataclass
class _Section:
    path: list[str]
    lines: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines).strip()


def _split_long(text: str, limit: int) -> list[str]:
    parts: list[str] = []
    current = ""
    for para in re.split(r"\n\s*\n", text):
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        while len(para) > limit:
            cut = para.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            parts.append(para[:cut])
            para = para[cut:].lstrip()
        current = para
    if current:
        parts.append(current)
    return parts


def chunk_note(note: Note, title: str) -> list[Chunk]:
    """Split on #-### headings, merge tiny sections, cap size, prefix '{title} > {path}'."""
    sections: list[_Section] = [_Section(path=[])]
    stack: list[tuple[int, str]] = []
    for line in note.body.splitlines():
        m = _HEADING.match(line)
        if m:
            level, name = len(m.group(1)), m.group(2)
            while stack and stack[-1][0] >= level:
                stack.pop()
            if level > 1:  # the H1 is the title itself
                stack.append((level, name))
            sections.append(_Section(path=[n for _, n in stack]))
            continue
        sections[-1].lines.append(line)

    merged: list[_Section] = []
    for sec in sections:
        if not sec.text:
            continue
        if merged and len(sec.text) < CHUNK_MIN_CHARS and len(merged[-1].text) < CHUNK_MAX_CHARS:
            label = " > ".join(sec.path)
            merged[-1].lines += ["", f"{label}:" if label else "", sec.text]
            continue
        merged.append(sec)

    chunks: list[Chunk] = []
    for sec in merged:
        heading = " > ".join(sec.path) or None
        prefix = f"{title} > {heading}" if heading else title
        for piece in _split_long(sec.text, CHUNK_MAX_CHARS):
            chunks.append(Chunk(len(chunks), heading, f"{prefix}\n{piece.strip()}"))
    if not chunks and title:
        chunks.append(Chunk(0, None, title))
    return chunks


def wikilinks(body: str) -> list[str]:
    out: list[str] = []
    for m in _WIKILINK.finditer(body):
        try:
            rel = normalise_rel(m.group(1).strip())
        except PathError:
            continue
        if rel not in out:
            out.append(rel)
    return out


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
