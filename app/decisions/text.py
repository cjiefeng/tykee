"""Normalisation for aliases and slugs (§8.1)."""

from __future__ import annotations

import re
import unicodedata

ALIAS_MAX = 40
SLUG_MAX = 40

_NON_WORD = re.compile(r"[^\w\s]+")
_SPACES = re.compile(r"\s+")
_SLUG_SEP = re.compile(r"[^\w]+")


def normalise(text: str) -> str:
    """NFKC, casefold, punctuation → space, whitespace collapsed. Works for CJK too."""
    t = unicodedata.normalize("NFKC", text).casefold().replace("_", " ")
    t = _NON_WORD.sub(" ", t)
    return _SPACES.sub(" ", t).strip()


def slugify(text: str) -> str:
    """'Weekend Activity!' → 'weekend-activity'. Empty input → ''."""
    t = unicodedata.normalize("NFKC", text).casefold().replace("_", "-")
    t = _SLUG_SEP.sub("-", t).strip("-")
    return t[:SLUG_MAX].strip("-")


def display_name_for(slug: str) -> str:
    words = slug.replace("-", " ").strip()
    return words[:1].upper() + words[1:]


def contains_phrase(haystack: str, needle: str) -> bool:
    """Whole-word containment on already-normalised strings (CJK has no word boundaries, so a
    plain substring test is used when the needle contains non-ASCII letters)."""
    if not needle:
        return False
    if not needle.isascii():
        return needle in haystack
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None
