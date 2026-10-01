"""Model text → Telegram HTML (§10). Claude writes plain text with a tiny markdown subset;
we escape everything, convert ``**bold**``, ``_italic_`` and ``[text](https://url)``, and split
so every chunk is ≤ 4096 chars *after* rendering and has balanced tags."""

from __future__ import annotations

import html
import re

TELEGRAM_LIMIT = 4096

_INLINE = re.compile(
    r"\[(?P<ltext>[^\]\n]+)\]\((?P<url>https?://[^\s)]+)\)"
    r"|\*\*(?P<bold>[^\n]+?)\*\*"
    r"|(?<!\w)_(?P<ital>[^_\n]+?)_(?!\w)"
)


def escape(text: str) -> str:
    return html.escape(text, quote=True)


def render(text: str) -> str:
    """Render one chunk of model text to Telegram HTML."""
    out: list[str] = []
    pos = 0
    for m in _INLINE.finditer(text):
        out.append(escape(text[pos : m.start()]))
        if m.group("url"):
            out.append(f'<a href="{escape(m.group("url"))}">{escape(m.group("ltext"))}</a>')
        elif m.group("bold"):
            out.append(f"<b>{escape(m.group('bold'))}</b>")
        else:
            out.append(f"<i>{escape(m.group('ital'))}</i>")
        pos = m.end()
    out.append(escape(text[pos:]))
    return "".join(out)


def _hard_split(piece: str, limit: int) -> list[str]:
    """Split a single over-long line on spaces, falling back to a hard cut."""
    parts: list[str] = []
    current = ""
    for word in piece.split(" "):
        candidate = f"{current} {word}" if current else word
        if len(render(candidate)) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        # A single word that alone is too long: cut the longest prefix that fits.
        while len(render(word)) > limit:
            lo, hi = 1, len(word)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if len(render(word[:mid])) <= limit:
                    lo = mid
                else:
                    hi = mid - 1
            parts.append(word[:lo])
            word = word[lo:]
        current = word
    if current:
        parts.append(current)
    return parts


def to_html_chunks(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split on line boundaries (then spaces) so each rendered chunk fits ``limit``."""
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(render(candidate)) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(render(line)) <= limit:
            current = line
        else:
            pieces = _hard_split(line, limit)
            chunks.extend(pieces[:-1])
            current = pieces[-1] if pieces else ""
    if current:
        chunks.append(current)
    return [render(c.strip("\n")) for c in chunks if c.strip()]
