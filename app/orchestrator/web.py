"""Web access (§7.5): Anthropic's server-side web search/fetch tools on the orchestrator call.

Code decides whether the tools are offered at all (master switch, daily search cap, budget);
Claude decides when to use them. Results come back inside the same response, so there is
nothing to execute here: just the tool definitions, the gate, and reading the result blocks.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from anthropic.types import ContentBlock, Message, ToolUnionParam

from app.db.database import Database
from app.db.repos import usage as usage_repo
from app.llm.client import budget_status
from app.settings import RuntimeSettings
from app.timeutil import local_day_start, to_sql, utcnow

WEB_TOOL_NAMES = frozenset({"web_search", "web_fetch"})
MAX_SOURCES = 2

WebOff = Literal["", "disabled", "daily_cap", "budget"]


@dataclass(frozen=True)
class WebStatus:
    on: bool
    reason: WebOff = ""  # why it's off; "" when on

    @property
    def temporarily_off(self) -> bool:
        """Off for a reason Claude should mention if live info is needed (not the master switch)."""
        return self.reason in ("daily_cap", "budget")


async def web_status(db: Database, s: RuntimeSettings, tz: ZoneInfo) -> WebStatus:
    """§7.5: the master switch, then the daily search cap, then the budget. Web tools are the
    first thing dropped when spend reaches ``budget.warn_ratio`` of either cap."""
    if not s.web_enabled or s.web_tool_versions is None:
        return WebStatus(False, "disabled")
    day = to_sql(local_day_start(utcnow(), tz))

    def _searches(conn: sqlite3.Connection) -> int:
        return usage_repo.web_searches_since(conn, day)

    if await db.read(_searches) >= s.web_daily_search_cap:
        return WebStatus(False, "daily_cap")
    b = await budget_status(db, s, tz)
    ratio = s.budget_warn_ratio
    if b.daily_spent >= ratio * b.daily_cap or b.monthly_spent >= ratio * b.monthly_cap:
        return WebStatus(False, "budget")
    return WebStatus(True)


def web_tools(s: RuntimeSettings) -> list[ToolUnionParam]:
    """Server tool definitions from settings; tool type strings are never hardcoded."""
    versions = s.web_tool_versions
    if versions is None:
        return []
    domains: dict[str, Any] = {}
    if s.web_allowed_domains:
        domains["allowed_domains"] = list(s.web_allowed_domains)
    elif s.web_blocked_domains:
        domains["blocked_domains"] = list(s.web_blocked_domains)
    search: dict[str, Any] = {
        "type": versions.web_search,
        "name": "web_search",
        "max_uses": s.web_search_max_uses,
        **domains,
    }
    if s.web_user_location is not None:
        search["user_location"] = s.web_user_location.model_dump(exclude_none=True)
    tools = [cast(ToolUnionParam, search)]
    if s.web_fetch_max_uses > 0:
        fetch: dict[str, Any] = {
            "type": versions.web_fetch,
            "name": "web_fetch",
            "max_uses": s.web_fetch_max_uses,
            "max_content_tokens": s.web_fetch_max_content_tokens,
            **domains,
        }
        tools.append(cast(ToolUnionParam, fetch))
    return tools


def used_web(content: Sequence[ContentBlock]) -> bool:
    return any(b.type == "server_tool_use" and b.name in WEB_TOOL_NAMES for b in content)


@dataclass(frozen=True)
class ServerCall:
    """One web search/fetch from a response, for the dashboard's conversation view and logs."""

    name: str
    input: dict[str, Any]
    result: str  # result URLs, or "error: <code>"
    error_code: str = ""


def server_calls(content: Sequence[ContentBlock]) -> list[ServerCall]:
    results: dict[str, tuple[str, str]] = {}
    for b in content:
        if b.type == "web_search_tool_result":
            if isinstance(b.content, list):
                results[b.tool_use_id] = (" ".join(r.url for r in b.content), "")
            else:
                results[b.tool_use_id] = (f"error: {b.content.error_code}", b.content.error_code)
        elif b.type == "web_fetch_tool_result":
            c = b.content
            if c.type == "web_fetch_tool_result_error":
                results[b.tool_use_id] = (f"error: {c.error_code}", c.error_code)
            else:
                results[b.tool_use_id] = (c.url, "")
    out: list[ServerCall] = []
    for b in content:
        if b.type == "server_tool_use" and b.name in WEB_TOOL_NAMES:
            raw = b.input if isinstance(b.input, dict) else {}
            result, code = results.get(b.id, ("pending", ""))
            out.append(ServerCall(b.name, raw, result, code))
    return out


def sources(message: Message) -> list[tuple[str, str]]:
    """(site, url) for the web search results the final text cites, in order, deduplicated."""
    seen: dict[str, str] = {}
    for b in message.content:
        if b.type != "text" or not b.citations:
            continue
        for c in b.citations:
            if c.type == "web_search_result_location" and c.url not in seen:
                seen[c.url] = urlsplit(c.url).hostname or c.url
    return [(site.removeprefix("www."), url) for url, site in seen.items()]


def with_sources(text: str, cited: Sequence[tuple[str, str]]) -> str:
    """§7.5: a cited answer has at most two source links. Claude usually writes them itself;
    if it didn't link anything, append the first cited sources."""
    if not cited or "](" in text:
        return text
    links = " · ".join(f"[{site}]({url})" for site, url in cited[:MAX_SOURCES])
    return f"{text}\n{links}"
