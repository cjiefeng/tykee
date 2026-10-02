"""Decision tools exposed to Claude (§7.3) and the router that executes them.

Inputs are validated with pydantic; any problem comes back to Claude as an ``is_error`` tool
result rather than an exception, so the loop can recover.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from anthropic.types import ToolParam
from pydantic import BaseModel, Field, ValidationError

from app.db.repos.users import UserRecord
from app.decisions.categories import Category
from app.decisions.engine import ExtraCandidate, PickRequest
from app.decisions.service import DecisionService

log = logging.getLogger(__name__)


# --- input models ----------------------------------------------------------------------------


class _Extra(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    tags: list[str]


class ResolveCategoryIn(BaseModel):
    phrase: str = Field(min_length=1, max_length=200)
    proposed_slug: str = Field(min_length=1, max_length=60)
    description: str = Field(min_length=1, max_length=300)
    proposed_tau_days: float = Field(gt=0)
    use_existing: str | None = None
    create_new: bool = False


class RandomPickIn(BaseModel):
    category: str
    n: int | None = Field(default=None, ge=1, le=5)
    for_users: str | None = None
    include_tags: list[str] = Field(default_factory=list)
    exclude_tags: list[str] = Field(default_factory=list)
    extra_candidates: list[_Extra] = Field(default_factory=list, max_length=15)


class CategoryIn(BaseModel):
    category: str


class AddOptionIn(BaseModel):
    category: str
    name: str = Field(min_length=1, max_length=120)
    tags: list[str] = Field(default_factory=list)
    owner: str = "shared"


class RecentDecisionsIn(BaseModel):
    category: str
    days: float = Field(default=14, gt=0, le=365)


# --- schemas ---------------------------------------------------------------------------------

_TAGS = {"type": "array", "items": {"type": "string"}}


def tool_definitions(user_slugs: Sequence[str]) -> list[ToolParam]:
    for_users = {
        "type": "string",
        "enum": [*user_slugs, "both"],
        "description": "Who the decision is for. Omit to use the chat default.",
    }
    return [
        {
            "name": "resolve_category",
            "description": (
                "Map the kind of decision being asked about to a canonical category. Call this "
                "before random_pick. If it returns status 'choose', call it again with either "
                "use_existing=<slug> (same kind of decision as an existing category) or "
                "create_new=true (a genuinely different kind)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "phrase": {
                        "type": "string",
                        "description": (
                            "The user's own words for the decision, e.g. 'eat tonight', 'makan', "
                            "'something to watch'."
                        ),
                    },
                    "proposed_slug": {
                        "type": "string",
                        "description": (
                            "Short generic lowercase-hyphenated decision type, e.g. 'dinner', "
                            "'movie', 'weekend-activity'. Never a specific item."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "One line describing this kind of decision.",
                    },
                    "proposed_tau_days": {
                        "type": "number",
                        "description": (
                            "How many days before repeating a choice feels fine: meals 2-4, "
                            "snacks/drinks 1, movies/shows 14-30, weekend activities 7-14, trips "
                            "90+."
                        ),
                    },
                    "use_existing": {
                        "type": "string",
                        "description": (
                            "Slug of an existing category to use (after status 'choose')."
                        ),
                    },
                    "create_new": {
                        "type": "boolean",
                        "description": "Create a new category (after status 'choose').",
                    },
                },
                "required": ["phrase", "proposed_slug", "description", "proposed_tau_days"],
            },
        },
        {
            "name": "random_pick",
            "description": (
                "Make the actual choice with a weighted random pick that avoids recent repeats "
                "and respects preferences. Always use this to choose; never choose yourself. "
                "For categories with few or no options, pass 3-8 extra_candidates that fit the "
                "request and what you know about the users."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Slug returned by resolve_category.",
                    },
                    "n": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5,
                        "description": "Number of picks; omit for the category default.",
                    },
                    "for_users": for_users,
                    "include_tags": {
                        **_TAGS,
                        "description": "Options must have all of these tags.",
                    },
                    "exclude_tags": {
                        **_TAGS,
                        "description": (
                            "Options with any of these tags are excluded, e.g. 'contains:peanut', "
                            "'spicy'."
                        ),
                    },
                    "extra_candidates": {
                        "type": "array",
                        "description": (
                            "Your own suggestions, added to the pool. Tag allergens as "
                            "'contains:<ingredient>'."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {"name": {"type": "string"}, "tags": _TAGS},
                            "required": ["name", "tags"],
                        },
                    },
                },
                "required": ["category"],
            },
        },
        {
            "name": "list_options",
            "description": "List the saved options for a category, with tags and owner.",
            "input_schema": {
                "type": "object",
                "properties": {"category": {"type": "string"}},
                "required": ["category"],
            },
        },
        {
            "name": "add_option",
            "description": (
                "Save a new option in a category (e.g. a new restaurant they mention liking)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "name": {"type": "string"},
                    "tags": _TAGS,
                    "owner": {"type": "string", "enum": [*user_slugs, "shared"]},
                },
                "required": ["category", "name", "tags"],
            },
        },
        {
            "name": "recent_decisions",
            "description": "What was chosen, rerolled or rejected recently in a category.",
            "input_schema": {
                "type": "object",
                "properties": {"category": {"type": "string"}, "days": {"type": "number"}},
                "required": ["category"],
            },
        },
    ]


# --- router ----------------------------------------------------------------------------------


@dataclass
class TurnContext:
    chat_id: int
    actor: UserRecord
    default_for_users: str
    tz: ZoneInfo
    last_picks: list[tuple[int, str]] = field(default_factory=list)  # (decision_id, name)


@dataclass(frozen=True)
class ToolOutcome:
    content: str
    is_error: bool = False


def _ok(payload: dict[str, Any]) -> ToolOutcome:
    return ToolOutcome(json.dumps(payload, ensure_ascii=False))


def _err(message: str) -> ToolOutcome:
    return ToolOutcome(json.dumps({"error": message}, ensure_ascii=False), is_error=True)


class ToolRouter:
    def __init__(self, decisions: DecisionService) -> None:
        self._d = decisions

    def definitions(self) -> list[ToolParam]:
        return tool_definitions(self._d.user_slugs)

    async def execute(self, name: str, raw: dict[str, Any], ctx: TurnContext) -> ToolOutcome:
        handler = {
            "resolve_category": self._resolve,
            "random_pick": self._pick,
            "list_options": self._list,
            "add_option": self._add,
            "recent_decisions": self._recent,
        }.get(name)
        if handler is None:
            return _err(f"unknown tool {name!r}")
        try:
            return await handler(raw, ctx)
        except ValidationError as e:
            return _err(f"invalid input: {e.errors(include_url=False)}")

    async def _category(self, name: str) -> Category | None:
        return await self._d.lookup(name)

    async def _resolve(self, raw: dict[str, Any], ctx: TurnContext) -> ToolOutcome:
        a = ResolveCategoryIn.model_validate(raw)
        r = await self._d.resolve(
            phrase=a.phrase,
            proposed_slug=a.proposed_slug,
            description=a.description,
            proposed_tau_days=a.proposed_tau_days,
            use_existing=a.use_existing,
            create_new=a.create_new,
        )
        if r.status == "error":
            return _err(r.error)
        if r.status == "choose":
            return _ok(
                {
                    "status": "choose",
                    "message": "No existing category has this name. If one below is the same "
                    "kind of decision, call again with use_existing=<slug>; otherwise call "
                    "again with create_new=true.",
                    "categories": [
                        {
                            "slug": e.slug,
                            "name": e.display_name,
                            "description": e.description,
                            "uses": e.uses,
                        }
                        for e in r.catalog
                    ],
                }
            )
        assert r.category is not None
        log.info("category resolved", extra={"slug": r.category.slug, "via": r.via})
        return _ok(
            {
                "status": r.status,
                "category": r.category.slug,
                "name": r.category.display_name,
                "default_n": r.category.default_n,
            }
        )

    async def _pick(self, raw: dict[str, Any], ctx: TurnContext) -> ToolOutcome:
        a = RandomPickIn.model_validate(raw)
        cat = await self._category(a.category)
        if cat is None:
            return _err(f"unknown category {a.category!r}; call resolve_category first")
        for_users = a.for_users or ctx.default_for_users
        if for_users != "both" and for_users not in self._d.user_slugs:
            return _err(f"for_users must be one of {[*self._d.user_slugs, 'both']}")
        req = PickRequest(
            category_id=cat.id,
            for_users=for_users,
            n=a.n,
            include_tags=a.include_tags,
            exclude_tags=a.exclude_tags,
            extra_candidates=[ExtraCandidate(x.name, x.tags) for x in a.extra_candidates],
        )
        result = await self._d.pick(cat, req, asked_by=ctx.actor.id, chat_id=ctx.chat_id)
        ctx.last_picks = [(p.decision_id, p.name) for p in result.picks]
        if not result.picks:
            return _ok(
                {
                    "picks": [],
                    "note": "No candidates left. Suggest extra_candidates, relax the tag filters, "
                    "or tell the user there's nothing that fits.",
                }
            )
        return _ok(
            {
                "category": cat.slug,
                "for_users": for_users,
                "picks": [{"name": p.name, "tags": list(p.tags)} for p in result.picks],
                "candidates_considered": result.considered,
                "note": (
                    "Buttons to accept, reroll or reject are attached to your reply automatically."
                ),
            }
        )

    async def _list(self, raw: dict[str, Any], ctx: TurnContext) -> ToolOutcome:
        a = CategoryIn.model_validate(raw)
        cat = await self._category(a.category)
        if cat is None:
            return _err(f"unknown category {a.category!r}")
        opts = await self._d.list_options(cat)
        return _ok(
            {
                "category": cat.slug,
                "options": [{"name": o.name, "tags": o.tags, "owner": o.owner} for o in opts],
            }
        )

    async def _add(self, raw: dict[str, Any], ctx: TurnContext) -> ToolOutcome:
        a = AddOptionIn.model_validate(raw)
        cat = await self._category(a.category)
        if cat is None:
            return _err(f"unknown category {a.category!r}; call resolve_category first")
        if a.owner != "shared" and a.owner not in self._d.user_slugs:
            return _err("owner must be a user slug or 'shared'")
        added = await self._d.add_option(cat, a.name, a.tags, a.owner)
        return _ok({"added": added, "note": "" if added else "already exists"})

    async def _recent(self, raw: dict[str, Any], ctx: TurnContext) -> ToolOutcome:
        a = RecentDecisionsIn.model_validate(raw)
        cat = await self._category(a.category)
        if cat is None:
            return _err(f"unknown category {a.category!r}")
        rows = await self._d.recent(cat, a.days)
        return _ok(
            {
                "category": cat.slug,
                "decisions": [
                    {
                        "when": r.created_at.astimezone(ctx.tz).strftime("%a %Y-%m-%d"),
                        "choice": r.choice_text,
                        "status": r.status,
                        "for": r.for_users,
                    }
                    for r in rows
                ],
            }
        )
