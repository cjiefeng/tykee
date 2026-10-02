"""Decision tools exposed to Claude (§7.3) and the router that executes them.

Inputs are validated with pydantic; any problem comes back to Claude as an ``is_error`` tool
result rather than an exception, so the loop can recover.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from anthropic.types import ToolParam
from pydantic import BaseModel, Field, ValidationError

from app.brain.memory import MemoryPolicyError, MemoryService
from app.brain.store import NoteError
from app.db.repos.users import UserRecord
from app.decisions.categories import Category
from app.decisions.engine import ExtraCandidate, PickRequest
from app.decisions.service import DecisionService
from app.orchestrator.escalation import Tier
from app.places import attributes as attrs
from app.places import pets as pets_mod
from app.places.recommend import (
    FoundAttribute,
    FoundCandidate,
    RecommendError,
    RecommendService,
    TurnState,
)
from app.places.service import PlaceService

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
    place_id: int | None = None


class RecordDecisionIn(BaseModel):
    category: str
    choice: str = Field(min_length=1, max_length=120)
    for_users: str | None = None
    place_id: int | None = None


class FindPlacesIn(BaseModel):
    category: str
    area: str | None = Field(None, max_length=100)
    near_maps_url: str | None = Field(None, max_length=2000)
    anchor_place_id: int | None = None
    must: list[str] = Field(default_factory=list, max_length=5)
    n: int | None = Field(default=None, ge=1, le=5)
    for_users: str | None = None


class _FoundAttr(BaseModel):
    key: str
    value: str
    quote: str = Field("", max_length=300)


class _Found(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    source_url: str = Field(min_length=1, max_length=2000)
    address: str | None = Field(None, max_length=200)
    area: str | None = Field(None, max_length=100)
    attributes: list[_FoundAttr] = Field(default_factory=list, max_length=5)


class SaveCandidatesIn(BaseModel):
    category: str
    candidates: list[_Found] = Field(min_length=1, max_length=5)


class SetAttributeIn(BaseModel):
    place_id: int
    key: str
    value: str
    evidence: str = Field("", max_length=300)


class RecentDecisionsIn(BaseModel):
    category: str
    days: float = Field(default=14, gt=0, le=365)


class SearchMemoryIn(BaseModel):
    query: str = Field(min_length=1, max_length=300)
    scope: Literal["me", "partner", "both", "shared"] | None = None
    k: int | None = Field(default=None, ge=1, le=12)


class ReadNoteIn(BaseModel):
    path: str


class WriteNoteIn(BaseModel):
    path: str
    mode: Literal["create", "append", "replace_section", "replace"]
    content: str = Field(min_length=1, max_length=4000)
    heading: str | None = None
    title: str | None = None
    tags: list[str] = Field(default_factory=list)
    add_avoid_tags: list[str] = Field(default_factory=list)
    remove_avoid_tags: list[str] = Field(default_factory=list)


class ProposeMemoryIn(BaseModel):
    owner: str
    content: str = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=300)
    topic: str = Field(min_length=1, max_length=60)
    source_url: str | None = Field(None, max_length=2000)


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
                    "place_id": {
                        "type": "integer",
                        "description": "If the option is a shared place, its place_id marker.",
                    },
                },
                "required": ["category", "name", "tags"],
            },
        },
        {
            "name": "record_decision",
            "description": (
                "Record a choice the users already made themselves (no pick needed), e.g. they "
                "shared a place and said they're eating there. Call resolve_category first. A "
                "reaction on their message confirms it, so keep any reply to a few words."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Slug returned by resolve_category.",
                    },
                    "choice": {"type": "string", "description": "What they chose."},
                    "for_users": for_users,
                    "place_id": {
                        "type": "integer",
                        "description": "The place_id from a ⟦place: … · place_id=N⟧ marker.",
                    },
                },
                "required": ["category", "choice"],
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


def place_tool_definitions(user_slugs: Sequence[str]) -> list[ToolParam]:
    keys = sorted(attrs.VALUES)
    return [
        {
            "name": "find_places",
            "description": (
                "Recommend places around an area from the places they know (ranked and picked in "
                "code; never pick yourself). "
                "Call resolve_category first (brunch, dinner, cafe…). Give exactly one of area "
                "(a neighbourhood, town or MRT station in their words, e.g. 'Tiong Bahru', "
                "'near home'), near_maps_url (a Maps link they pasted) or anchor_place_id (a "
                "place_id marker: 'near X'). must lists required attributes, e.g. "
                "['pet_friendly'] when they ask for pet friendly or mention a pet by name. "
                "Present the picks in the order and numbering returned; buttons are attached "
                "automatically. If it says suggest_web, follow its note."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "Slug from resolve_category."},
                    "area": {"type": "string"},
                    "near_maps_url": {"type": "string"},
                    "anchor_place_id": {"type": "integer"},
                    "must": {"type": "array", "items": {"type": "string", "enum": keys}},
                    "n": {"type": "integer", "minimum": 1, "maximum": 5},
                    "for_users": {
                        "type": "string",
                        "enum": [*user_slugs, "both"],
                        "description": "Who it's for. Omit to use the chat default.",
                    },
                },
                "required": ["category"],
            },
        },
        {
            "name": "save_place_candidates",
            "description": (
                "After find_places said suggest_web and you searched the web: save the places "
                "you found (only real places from the results, in the requested area) so they "
                "can be picked and remembered. Attributes are labels from the page, e.g. "
                "{key: 'pet_friendly', value: 'outdoor_only', quote: 'dogs welcome on our "
                "patio'}; values: pet_friendly yes|outdoor_only|no|unknown, others yes|no|"
                "unknown. Returns the new picks, numbered after the known ones."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "candidates": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "source_url": {
                                    "type": "string",
                                    "description": "The page it came from.",
                                },
                                "address": {"type": "string"},
                                "area": {"type": "string"},
                                "attributes": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "key": {"type": "string", "enum": keys},
                                            "value": {"type": "string"},
                                            "quote": {"type": "string"},
                                        },
                                        "required": ["key", "value"],
                                    },
                                },
                            },
                            "required": ["name", "source_url"],
                        },
                    },
                },
                "required": ["category", "candidates"],
            },
        },
        {
            "name": "set_place_attribute",
            "description": (
                "Record what one of the users says about a place they know, e.g. 'Merci Marcel "
                "only allows dogs outside' → place_id, key 'pet_friendly', value "
                "'outdoor_only', evidence in their words (size limits like 'small dogs only' "
                "go here). Only from what the users said, never from web results; it beats "
                "anything found online."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "place_id": {"type": "integer"},
                    "key": {"type": "string", "enum": keys},
                    "value": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["place_id", "key", "value"],
            },
        },
    ]


def memory_tool_definitions(user_slugs: Sequence[str]) -> list[ToolParam]:
    owners = [*user_slugs, "shared"]
    return [
        {
            "name": "search_memory",
            "description": (
                "Search the second brain (notes about the users, places, preferences). Write the "
                "query in plain English plus any original local terms, e.g. 'takeaway food "
                "tapao' or 'dessert 甜品'. Use before suggesting things where past preferences "
                "matter."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "scope": {
                        "type": "string",
                        "enum": ["me", "partner", "both", "shared"],
                        "description": "Whose notes: the asker's, the other person's, both, or "
                        "shared only. Omit for the chat default.",
                    },
                    "k": {"type": "integer", "minimum": 1, "maximum": 12},
                },
                "required": ["query"],
            },
        },
        {
            "name": "read_note",
            "description": "Read a whole note by path (from search results or pinned notes). "
            "Always read before changing a note.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "name": "write_note",
            "description": (
                "Save something the user EXPLICITLY asked you to remember or forget. Paths: "
                "people/<user>.md (profile + hard constraints), memories/<user>/<topic>.md, "
                "shared/household.md, shared/places/<name>.md, shared/topics/<name>.md. Modes: "
                "create (new note), append (optionally under a heading), replace_section "
                "(rewrite one section, e.g. to fix a contradiction), replace (rewrite the "
                "whole body, e.g. to forget a line). For allergies and strong dislikes, also "
                "set add_avoid_tags on people/<user>.md, e.g. ['contains:coriander'], so picks "
                "exclude them automatically."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "mode": {
                        "type": "string",
                        "enum": ["create", "append", "replace_section", "replace"],
                    },
                    "content": {"type": "string", "description": "Markdown, plain English."},
                    "heading": {"type": "string"},
                    "title": {"type": "string", "description": "Title for a new note."},
                    "tags": _TAGS,
                    "add_avoid_tags": _TAGS,
                    "remove_avoid_tags": _TAGS,
                },
                "required": ["path", "mode", "content"],
            },
        },
        {
            "name": "propose_memory",
            "description": (
                "Suggest remembering a durable fact the user did NOT explicitly ask you to save "
                "(e.g. 'I'm off seafood this month' said in passing). It goes to an approval "
                "inbox, not straight into memory. Don't propose trivia, one-off moods, or "
                "anything about health beyond diet, finances, work or the relationship."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "enum": owners},
                    "content": {"type": "string", "description": "One plain-English sentence."},
                    "reason": {"type": "string", "description": "What in the chat suggested it."},
                    "topic": {
                        "type": "string",
                        "description": "Short note name to file it under, e.g. 'food', 'drinks'.",
                    },
                    "source_url": {
                        "type": "string",
                        "description": "If the fact came from the web, the page URL.",
                    },
                },
                "required": ["owner", "content", "reason", "topic"],
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
    is_group: bool = False
    source: str = ""  # provenance for notes written this turn, e.g. telegram:<chat_id>
    last_picks: list[tuple[int, str]] = field(default_factory=list)  # (decision_id, name)
    recorded: list[tuple[int, int | None]] = field(default_factory=list)  # (decision, place)
    web_used: bool = False  # a web search/fetch ran this turn → memory writes need approval (§7.5)
    web_on: bool = False  # web tools offered this turn (find_places may leave slots for the web)
    recommend: TurnState | None = None  # the turn's find_places, for save_place_candidates
    tier: Tier = "default"  # §7.1 model tier for this turn's calls
    max_tokens: int | None = None  # None → llm.max_tokens; escalation.max_tokens when escalated


@dataclass(frozen=True)
class ToolOutcome:
    content: str
    is_error: bool = False


def _ok(payload: dict[str, Any]) -> ToolOutcome:
    return ToolOutcome(json.dumps(payload, ensure_ascii=False))


def _err(message: str) -> ToolOutcome:
    return ToolOutcome(json.dumps({"error": message}, ensure_ascii=False), is_error=True)


class ToolRouter:
    def __init__(
        self,
        decisions: DecisionService,
        memory: MemoryService | None = None,
        places: PlaceService | None = None,
        recommend: RecommendService | None = None,
    ) -> None:
        self._d = decisions
        self._m = memory
        self._p = places
        self._r = recommend

    def definitions(self) -> list[ToolParam]:
        tools = tool_definitions(self._d.user_slugs)
        if self._r is not None:
            tools += place_tool_definitions(self._d.user_slugs)
        if self._m is not None:
            tools += memory_tool_definitions(self._d.user_slugs)
        return tools

    async def execute(self, name: str, raw: dict[str, Any], ctx: TurnContext) -> ToolOutcome:
        handler = {
            "resolve_category": self._resolve,
            "random_pick": self._pick,
            "list_options": self._list,
            "add_option": self._add,
            "recent_decisions": self._recent,
            "record_decision": self._record,
            **(
                {
                    "find_places": self._find_places,
                    "save_place_candidates": self._save_candidates,
                    "set_place_attribute": self._set_attribute,
                }
                if self._r is not None
                else {}
            ),
            **(
                {
                    "search_memory": self._search_memory,
                    "read_note": self._read_note,
                    "write_note": self._write_note,
                    "propose_memory": self._propose_memory,
                }
                if self._m is not None
                else {}
            ),
        }.get(name)
        if handler is None:
            return _err(f"unknown tool {name!r}")
        try:
            return await handler(raw, ctx)
        except ValidationError as e:
            return _err(f"invalid input: {e.errors(include_url=False)}")
        except (MemoryPolicyError, NoteError, RecommendError, attrs.AttrError) as e:
            return _err(str(e))

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
        ctx.recommend = None
        constraints = list(result.hard_excluded)
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
                **({"excluded_by_constraints": constraints} if constraints else {}),
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
        place_id = None
        if a.place_id is not None:
            place = await self._p.get(a.place_id) if self._p is not None else None
            if place is None:
                return _err(f"unknown place_id {a.place_id}")
            place_id = place.id
        added = await self._d.add_option(cat, a.name, a.tags, a.owner, place_id)
        return _ok({"added": added, "note": "" if added else "already exists"})

    async def _record(self, raw: dict[str, Any], ctx: TurnContext) -> ToolOutcome:
        a = RecordDecisionIn.model_validate(raw)
        cat = await self._category(a.category)
        if cat is None:
            return _err(f"unknown category {a.category!r}; call resolve_category first")
        for_users = a.for_users or ctx.default_for_users
        if for_users != "both" and for_users not in self._d.user_slugs:
            return _err(f"for_users must be one of {[*self._d.user_slugs, 'both']}")
        choice, place_id = a.choice.strip(), None
        if a.place_id is not None:
            place = await self._p.get(a.place_id) if self._p is not None else None
            if place is None:
                return _err(f"unknown place_id {a.place_id}")
            choice, place_id = place.name, place.id
        r = await self._d.record_user(
            cat,
            choice=choice,
            for_users=for_users,
            asked_by=ctx.actor.id,
            chat_id=ctx.chat_id,
            place_id=place_id,
        )
        if r.created:
            ctx.recorded.append((r.decision_id, place_id))
            if place_id is not None and self._p is not None:
                await self._p.visit(place_id)
            if self._m is not None:
                await self._m.log_decision(
                    f"- {datetime.now(ctx.tz):%H:%M} · {cat.display_name} · **{choice}** · "
                    f"for {for_users} · 📍 by {ctx.actor.display_name}"
                )
        log.info("decision recorded", extra={"slug": cat.slug, "new": r.created})
        return _ok(
            {
                "recorded": r.created,
                "category": cat.slug,
                "choice": choice,
                "note": "Done; a reaction confirms it."
                if r.created
                else "Already recorded earlier; nothing changed.",
            }
        )

    # --- place recommendations (§10.6) -------------------------------------------------------

    async def _pet_emoji(self) -> str:
        return pets_mod.emoji(await self._m.pets()) if self._m is not None else "🐾"

    async def _picks_payload(self, state: TurnState, shown: Sequence[Any]) -> list[dict[str, Any]]:
        assert self._r is not None
        emoji = await self._pet_emoji()
        must = state.result.request.must
        return [await self._r.describe(x, must, emoji) for x in shown]

    async def _find_places(self, raw: dict[str, Any], ctx: TurnContext) -> ToolOutcome:
        assert self._r is not None
        a = FindPlacesIn.model_validate(raw)
        cat = await self._category(a.category)
        if cat is None:
            return _err(f"unknown category {a.category!r}; call resolve_category first")
        for_users = a.for_users or ctx.default_for_users
        if for_users != "both" and for_users not in self._d.user_slugs:
            return _err(f"for_users must be one of {[*self._d.user_slugs, 'both']}")
        centre = await self._r.locate(
            area=a.area, near_maps_url=a.near_maps_url, anchor_place_id=a.anchor_place_id
        )
        result = await self._r.find(
            cat,
            centre,
            for_users=for_users,
            must=a.must,
            n=a.n,
            asked_by=ctx.actor.id,
            chat_id=ctx.chat_id,
            web=ctx.web_on,
            anchor_place_id=a.anchor_place_id,
        )
        ctx.recommend = TurnState(result, cat)
        ctx.last_picks = [(x.decision_id, x.item.place.name) for x in result.picks]
        payload: dict[str, Any] = {
            "area": centre.label,
            "category": cat.slug,
            "picks": await self._picks_payload(ctx.recommend, result.picks),
            "known_places_considered": result.considered,
        }
        if result.suggest_web and ctx.web_on and result.slots_left:
            s = await self._r.settings()
            words = [k.replace("_", " ") for k in result.request.must]
            query = " ".join([*words, cat.display_name.lower(), centre.label])
            payload["suggest_web"] = True
            payload["note"] = (
                f"Find {result.slots_left} new place(s) on the web: at most "
                f"{s.recommend_max_web_searches} web_search calls, e.g. '{query}'. Then call "
                "save_place_candidates with what you found (name, source_url, address or area, "
                "attributes with a quote). Then reply with all picks, one line each."
            )
        elif result.slots_left:
            payload["note"] = (
                f"Only {len(result.picks)} known place(s) fit"
                + ("" if ctx.web_on else " and web lookups are off")
                + ". Say so briefly; don't invent places."
            )
        else:
            payload["note"] = (
                "Reply with one short line per pick, keeping the numbers; use each pick's facts "
                "(see 'line') and its [Map](maps_url) link. Label pet info as given."
            )
        return _ok(payload)

    async def _save_candidates(self, raw: dict[str, Any], ctx: TurnContext) -> ToolOutcome:
        assert self._r is not None
        a = SaveCandidatesIn.model_validate(raw)
        state = ctx.recommend
        cat = await self._category(a.category)
        if state is None or cat is None or cat.id != state.category.id:
            return _err("call find_places for this category first")
        found = [
            FoundCandidate(
                c.name,
                c.source_url,
                c.address,
                c.area,
                [FoundAttribute(x.key, x.value, x.quote) for x in c.attributes],
            )
            for c in a.candidates
        ]
        for c in found:
            for x in c.attributes:
                attrs.validate(x.key, x.value)
        shown, skipped = await self._r.save_found(
            state, found, asked_by=ctx.actor.id, chat_id=ctx.chat_id
        )
        ctx.last_picks += [(x.decision_id, x.item.place.name) for x in shown]
        return _ok(
            {
                "new_picks": await self._picks_payload(state, shown),
                **({"skipped": skipped} if skipped else {}),
                "all_picks_in_order": [name for _, name in ctx.last_picks],
                "note": "Reply with every pick in order, one short line each, numbered as given. "
                "Web-sourced labels must say they're unverified (as in 'line').",
            }
        )

    async def _set_attribute(self, raw: dict[str, Any], ctx: TurnContext) -> ToolOutcome:
        assert self._r is not None
        if ctx.web_used:
            return _err(
                "Web results were used this turn; set_place_attribute only records what the "
                "users said themselves."
            )
        a = SetAttributeIn.model_validate(raw)
        place = await self._r.places.get(a.place_id)
        if place is None:
            return _err(f"unknown place_id {a.place_id}")
        attr = await self._r.places.set_attribute(
            place.id, a.key, a.value, source="user", evidence=a.evidence or None
        )
        return _ok({"saved": True, "place": place.name, "key": attr.key, "value": attr.value})

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

    # --- memory tools (§6.5-6.7) -------------------------------------------------------------

    async def _search_memory(self, raw: dict[str, Any], ctx: TurnContext) -> ToolOutcome:
        assert self._m is not None
        a = SearchMemoryIn.model_validate(raw)
        scope = a.scope or ("both" if ctx.is_group else "me")
        hits = await self._m.search(a.query, asker=ctx.actor.slug, scope=scope, k=a.k)
        return _ok(
            {
                "scope": scope,
                "results": [
                    {
                        "path": h.path,
                        "title": h.title,
                        "heading": h.heading,
                        "snippet": h.snippet,
                        "owner": h.owner,
                        **({"via": "linked note"} if h.via == "link" else {}),
                    }
                    for h in hits
                ],
            }
        )

    async def _read_note(self, raw: dict[str, Any], ctx: TurnContext) -> ToolOutcome:
        assert self._m is not None
        a = ReadNoteIn.model_validate(raw)
        rel, note = await self._m.read(a.path, asker=ctx.actor.slug, is_group=ctx.is_group)
        return _ok(
            {
                "path": rel,
                "owner": note.owner,
                "pinned": note.pinned,
                "tags": note.meta.get("tags") or [],
                "avoid_tags": note.avoid_tags,
                "body": note.body,
            }
        )

    async def _write_note(self, raw: dict[str, Any], ctx: TurnContext) -> ToolOutcome:
        assert self._m is not None
        if ctx.web_used:
            # §7.5: web content is untrusted; nothing reaches the vault without approval.
            return _err(
                "Web results were used this turn, so notes can't be written directly. "
                "Use propose_memory (with source_url if it's a web fact); it goes to the inbox."
            )
        a = WriteNoteIn.model_validate(raw)
        result = await self._m.write(
            a.path,
            mode=a.mode,
            content=a.content,
            heading=a.heading,
            title=a.title,
            tags=a.tags,
            add_avoid_tags=a.add_avoid_tags,
            remove_avoid_tags=a.remove_avoid_tags,
            source=ctx.source,
        )
        return _ok({"saved": result.path, "created": result.created})

    async def _propose_memory(self, raw: dict[str, Any], ctx: TurnContext) -> ToolOutcome:
        assert self._m is not None
        a = ProposeMemoryIn.model_validate(raw)
        web = bool(a.source_url) or ctx.web_used
        item = await self._m.propose(
            owner=a.owner,
            content=a.content,
            reason=a.reason,
            topic=a.topic,
            source=f"web:{a.source_url}" if a.source_url else ctx.source,
            force_review=web,  # §7.5: web facts always wait for approval
        )
        return _ok(
            {
                "status": item.status,
                "note": "Saved."
                if item.status == "approved"
                else "Queued for approval; don't tell the user it's remembered yet.",
            }
        )
