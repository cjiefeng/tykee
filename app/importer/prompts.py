"""Prompts and output schemas for the import's Opus-tier calls (§15.3): per-window extraction
(batch), category design from all decision episodes (§15.3.2), and merging facts into notes.
Every prompt carries the safe-topic rules (§15.4)."""

from __future__ import annotations

from typing import Any

from app.extraction.schema import SAFE_TOPIC_RULES

_STR = {"type": "string"}
_NUM = {"type": "number"}
_STRS = {"type": "array", "items": _STR}

EXTRACT_SYSTEM = """\
You read a stretch of a couple's past Telegram chat for their decision bot, Tykee, which is \
being set up and should know them from day one. Users (use these slugs as owner / for_users / \
by): {users}. Lines from anyone else are labelled "other": use them as context only. They never \
own facts, and nothing personal about them is extracted. Use "shared" for things about both \
users or the household, and "both" for decisions made for both.

{rules}

If the transcript has a "--- new ---" line, extract only from the messages after it; the lines \
before it are context already processed.

- episodes: every decision episode: a stretch where they discussed and made (or failed to make) \
a choice: what to eat, which movie, where to go on Saturday, which sofa to buy. category_phrase \
is the kind of decision in their own words ("dinner", "makan where", "weekend plans"); \
phrases_seen lists every phrasing of it in the chat (English, Singlish or Chinese, verbatim). \
options_considered: each option raised, who raised it (by), its stance ("proposed", \
"rejected" or "chosen") and the reason given, if any ("" otherwise). outcome is "chosen", \
"undecided" or "abandoned"; choice is the chosen option or "". ts is the ISO local time of the \
decision as shown in the transcript. quotes: up to 3 short verbatim snippets (under 100 \
characters) that show the decision. confidence 0-1 that this really was a decision episode.
- facts: durable preferences, places liked or disliked, constraints (allergies, diet), \
routines: one plain-English sentence each, keeping local terms ("prefers to tapao (takeaway) \
on weekdays"). quote is a short verbatim snippet, ts the ISO local time it was said. Skip moods \
and one-offs.
- options: specific named options mentioned for a kind of decision (a restaurant, a dish, a \
show), with sentiment -1..1 (how much they seemed to like it) and a few lowercase tags \
(cuisine, genre, area).
- ⟦place: Name · …⟧ after a link marks a Google Maps place someone shared: use Name exactly as \
the option name or choice. "⟦location shared⟧" is an unnamed location or a home: never extract \
anything about it.
Return empty lists when there's nothing."""


CATEGORIES_SYSTEM = """\
You design the decision categories for Tykee, a couple's decision bot, from every decision \
episode found in six months of their chat. A category is a recurring kind of decision the bot \
will be asked to pick for (Tykee picks randomly among a category's options, weighted by \
preference and how recently each was chosen).

Rules:
- A category is a recurring kind of decision they actually make. Granularity follows how they \
decide: split "lunch" from "dinner" only if they decide them differently; keep "weekend \
activity" as one category unless the episodes clearly show distinct kinds.
- Minimum support: at least 3 episodes, or 2 that ended with a choice. Anything below folds \
into a broader category or goes to unmapped.
- Aim for 5 to 25 categories; no near-duplicates.
- If an existing category (listed below) is the same kind of decision, reuse its exact slug \
instead of creating a new one.
- slug: lowercase words joined by hyphens. display_name: short title case. description: one \
line saying what is being chosen.
- aliases: the actual phrasings seen for this kind of decision, including Singlish and Chinese, \
so the bot recognises them; no alias may belong to two categories.
- recency_tau_days: how many days before repeating a choice feels fine, from the observed \
cadence (meals 2-4, takeaway 3-7, movies or shows 30-120, weekend activities 7-21, big \
purchases 90-365). default_n: how many options to suggest at once (1 for meals, up to 3 for \
browsing-type decisions). allow_generated: true if the bot may suggest options never mentioned \
before (e.g. new restaurants), false for closed sets.
- Map every episode id to exactly one category in episode_ids, or list it in unmapped with a \
short why (e.g. "one-off, no recurring pattern").
- Never create a category in an excluded domain below, even if such episodes slipped through: \
put those episodes in unmapped with why "out_of_scope".

{rules}"""


NOTES_SYSTEM = """\
You write the long-term memory notes Tykee, a couple's decision bot, keeps about {who}, from \
facts extracted from six months of their chat. Each fact has an id, date, type, statement, \
quote and confidence.

- Merge duplicates and near-duplicates into one line. When facts conflict, the newer one wins; \
mention the change only if it's still useful ("used to love mala, now finds it too heavy").
- Group lines into a few topic notes (e.g. "food", "places", "entertainment", "routines", \
"shopping"); topic is one or two plain words. Put hard constraints (allergies, dietary rules, \
strong dislikes that must never be suggested) in the profile note: target "profile".
- Each line is one short plain-English sentence, keeping local terms. List the ids of the facts \
it is based on, and a confidence 0-1 (lower for a single weak mention).
- Drop anything that falls under the excluded domains below, and anything about people other \
than {who}.

{rules}"""


def extract_system(users: str) -> str:
    return EXTRACT_SYSTEM.format(users=users, rules=SAFE_TOPIC_RULES)


def categories_system() -> str:
    return CATEGORIES_SYSTEM.format(rules=SAFE_TOPIC_RULES)


def notes_system(who: str) -> str:
    return NOTES_SYSTEM.format(who=who, rules=SAFE_TOPIC_RULES)


CATEGORIES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "categories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slug": _STR,
                    "display_name": _STR,
                    "description": _STR,
                    "recency_tau_days": _NUM,
                    "default_n": {"type": "integer"},
                    "allow_generated": {"type": "boolean"},
                    "aliases": _STRS,
                    "episode_ids": _STRS,
                },
                "required": [
                    "slug",
                    "display_name",
                    "description",
                    "recency_tau_days",
                    "default_n",
                    "allow_generated",
                    "aliases",
                    "episode_ids",
                ],
                "additionalProperties": False,
            },
        },
        "unmapped": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"episode_id": _STR, "why": _STR},
                "required": ["episode_id", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["categories", "unmapped"],
    "additionalProperties": False,
}

NOTES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "enum": ["profile", "topic"]},
                    "topic": _STR,
                    "lines": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": _STR,
                                "fact_ids": _STRS,
                                "confidence": _NUM,
                            },
                            "required": ["text", "fact_ids", "confidence"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["target", "topic", "lines"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["notes"],
    "additionalProperties": False,
}
