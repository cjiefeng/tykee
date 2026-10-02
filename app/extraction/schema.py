"""Extraction output shared by the memory harvester (§10.4) and, in M5, the bootstrap import
(§15.3.1): decision episodes, facts and options, under the safe-topic rules of §15.4."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

SAFE_TOPIC_RULES = """\
Only extract things in these areas: food & drink, places, entertainment, activities, outings & \
trips, shopping preferences (what to buy, brands, styles), routines/schedules, dietary \
restrictions & allergies.

Never extract, and don't describe, anything about: health (conditions, medication, doctor \
visits, mental health; only allergies and diet are allowed), money (salaries, budgets, savings, \
investments, debts, bills, prices), work (jobs, colleagues, office matters), relationship talk \
(disagreements, feelings, intimacy, family conflicts), or personal details about third parties. \
Count such discussions in skipped_out_of_scope and produce nothing else for them. If an allowed \
item mentions money ("the cheaper one"), keep the choice and drop the money detail."""

_STR = {"type": "string"}
_NUM = {"type": "number"}

EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "episodes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "summary": _STR,
                    "category_phrase": _STR,
                    "phrases_seen": {"type": "array", "items": _STR},
                    "for_users": _STR,
                    "options_considered": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": _STR,
                                "by": _STR,
                                "stance": _STR,
                                "reason": _STR,
                            },
                            "required": ["name", "by", "stance", "reason"],
                            "additionalProperties": False,
                        },
                    },
                    "outcome": {"type": "string", "enum": ["chosen", "undecided", "abandoned"]},
                    "choice": _STR,
                    "ts": _STR,
                    "quotes": {"type": "array", "items": _STR},
                    "confidence": _NUM,
                },
                "required": [
                    "summary",
                    "category_phrase",
                    "phrases_seen",
                    "for_users",
                    "options_considered",
                    "outcome",
                    "choice",
                    "ts",
                    "quotes",
                    "confidence",
                ],
                "additionalProperties": False,
            },
        },
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "owner": _STR,
                    "type": {
                        "type": "string",
                        "enum": ["preference", "place", "fact", "constraint"],
                    },
                    "statement": _STR,
                    "quote": _STR,
                    "ts": _STR,
                    "confidence": _NUM,
                },
                "required": ["owner", "type", "statement", "quote", "ts", "confidence"],
                "additionalProperties": False,
            },
        },
        "options": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category_phrase": _STR,
                    "name": _STR,
                    "tags": {"type": "array", "items": _STR},
                    "sentiment": _NUM,
                },
                "required": ["category_phrase", "name", "tags", "sentiment"],
                "additionalProperties": False,
            },
        },
        "skipped_out_of_scope": {"type": "integer"},
    },
    "required": ["episodes", "facts", "options", "skipped_out_of_scope"],
    "additionalProperties": False,
}


def _clamp01(v: float) -> float:
    return min(max(v, 0.0), 1.0)


class ConsideredOption(BaseModel):
    name: str
    by: str = ""
    stance: str = ""
    reason: str = ""


class Episode(BaseModel):
    summary: str
    category_phrase: str
    phrases_seen: list[str] = Field(default_factory=list)
    for_users: str = "both"
    options_considered: list[ConsideredOption] = Field(default_factory=list)
    outcome: Literal["chosen", "undecided", "abandoned"]
    choice: str = ""
    ts: str = ""
    quotes: list[str] = Field(default_factory=list)
    confidence: float = 0.0

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, v: float) -> float:
        return _clamp01(v)


class Fact(BaseModel):
    owner: str
    type: Literal["preference", "place", "fact", "constraint"] = "fact"
    statement: str
    quote: str = ""
    ts: str = ""
    confidence: float = 0.0

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, v: float) -> float:
        return _clamp01(v)


class OptionSeen(BaseModel):
    category_phrase: str
    name: str
    tags: list[str] = Field(default_factory=list)
    sentiment: float = 0.0


class Extraction(BaseModel):
    episodes: list[Episode] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    options: list[OptionSeen] = Field(default_factory=list)
    skipped_out_of_scope: int = 0
