"""The only code allowed to call the Claude API (§7.0).

Every call: resolve model from settings → budget check → Messages API call (timeout, SDK
exponential-backoff retries on 408/409/429/5xx/overloaded) → ``usage`` row with computed cost.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

import anthropic
from anthropic.types import Message, MessageParam, TextBlockParam, ToolParam

from app.db.database import Database
from app.db.repos import usage as usage_repo
from app.llm.pricing import cost_usd
from app.settings import ModelRole, RuntimeSettings, SettingsStore
from app.timeutil import local_day_start, local_month_start, to_sql, utcnow

log = logging.getLogger(__name__)

Purpose = Literal["chat", "judge", "summary", "import_extract", "import_consolidate"]

INTERACTIVE_TIMEOUT_S = 30.0
CONSOLIDATION_TIMEOUT_S = 120.0
INTERACTIVE_MAX_RETRIES = 2


class LLMError(Exception):
    """Base for failures callers should turn into fallback behaviour (§8.5)."""


class LLMUnavailable(LLMError):
    """No API key, auth failure, or the API kept failing after retries."""


class BudgetExceeded(LLMError):
    def __init__(self, period: Literal["daily", "monthly"], spent: float, cap: float) -> None:
        super().__init__(f"{period} budget exhausted: ${spent:.4f} of ${cap:.2f}")
        self.period = period
        self.spent = spent
        self.cap = cap


@dataclass
class LLMRequest:
    purpose: Purpose
    model_role: ModelRole
    system: Sequence[TextBlockParam]
    messages: Sequence[MessageParam]
    max_tokens: int | None = None  # None → settings llm.max_tokens
    tools: Sequence[ToolParam] = field(default_factory=tuple)
    user_id: int | None = None
    chat_id: int | None = None
    import_job_id: int | None = None
    timeout_s: float = INTERACTIVE_TIMEOUT_S
    max_retries: int = INTERACTIVE_MAX_RETRIES


@dataclass(frozen=True)
class LLMResponse:
    message: Message
    model: str
    cost_usd: float

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.message.content if b.type == "text")


class LLMClient(Protocol):
    @property
    def configured(self) -> bool: ...

    async def complete(self, req: LLMRequest) -> LLMResponse: ...


@dataclass(frozen=True)
class BudgetStatus:
    daily_spent: float
    monthly_spent: float
    daily_cap: float
    monthly_cap: float


async def budget_status(db: Database, settings: RuntimeSettings, tz: ZoneInfo) -> BudgetStatus:
    now = utcnow()
    day_since = to_sql(local_day_start(now, tz))
    month_since = to_sql(local_month_start(now, tz))

    def _q(conn: sqlite3.Connection) -> tuple[float, float]:
        return usage_repo.cost_since(conn, day_since), usage_repo.cost_since(conn, month_since)

    daily, monthly = await db.read(_q)
    return BudgetStatus(daily, monthly, settings.budget_daily_usd, settings.budget_monthly_usd)


def check_budget(status: BudgetStatus) -> None:
    if status.monthly_spent >= status.monthly_cap:
        raise BudgetExceeded("monthly", status.monthly_spent, status.monthly_cap)
    if status.daily_spent >= status.daily_cap:
        raise BudgetExceeded("daily", status.daily_spent, status.daily_cap)


class AnthropicLLMClient:
    def __init__(
        self,
        *,
        api_key: str,
        db: Database,
        settings: SettingsStore,
        tz: ZoneInfo,
        http_client: anthropic.DefaultAsyncHttpxClient | None = None,
    ) -> None:
        self._db = db
        self._settings = settings
        self._tz = tz
        # Never construct the SDK without an explicit key: it would fall back to other
        # credential sources (profiles, env tokens), which we don't want at runtime.
        self._client: anthropic.AsyncAnthropic | None = (
            anthropic.AsyncAnthropic(api_key=api_key, http_client=http_client) if api_key else None
        )
        self.auth_failed = False

    @property
    def configured(self) -> bool:
        return self._client is not None and not self.auth_failed

    async def complete(self, req: LLMRequest) -> LLMResponse:
        if self._client is None or self.auth_failed:
            raise LLMUnavailable("ANTHROPIC_API_KEY missing or rejected")
        settings = await self._settings.load()
        check_budget(await budget_status(self._db, settings, self._tz))

        model = settings.model_for(req.model_role)
        client = self._client.with_options(timeout=req.timeout_s, max_retries=req.max_retries)
        try:
            message = await client.messages.create(
                model=model,
                max_tokens=req.max_tokens or settings.llm_max_tokens,
                system=list(req.system),
                messages=list(req.messages),
                tools=list(req.tools) if req.tools else anthropic.omit,
            )
        except anthropic.AuthenticationError as e:
            self.auth_failed = True
            log.error("anthropic auth failed; switching to fallback mode")
            raise LLMUnavailable("API key rejected") from e
        except anthropic.APIError as e:
            log.warning(
                "anthropic call failed",
                extra={"purpose": req.purpose, "model": model, "error": type(e).__name__},
            )
            raise LLMUnavailable(type(e).__name__) from e

        u = message.usage
        cache_read = u.cache_read_input_tokens or 0
        cache_write = u.cache_creation_input_tokens or 0
        price = settings.pricing.get(model)
        if price is None:
            log.warning("no pricing for model; cost recorded as 0", extra={"model": model})
        cost = cost_usd(
            price,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )
        row = usage_repo.UsageRow(
            user_id=req.user_id,
            purpose=req.purpose,
            chat_id=req.chat_id,
            import_job_id=req.import_job_id,
            model=model,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            cost_usd=cost,
            created_at=to_sql(utcnow()),
        )
        await self._db.write(lambda conn: usage_repo.insert(conn, row))
        log.info(
            "llm call",
            extra={
                "purpose": req.purpose,
                "model": model,
                "in": u.input_tokens,
                "out": u.output_tokens,
                "cache_read": cache_read,
                "cache_write": cache_write,
                "cost_usd": round(cost, 6),
                "stop": message.stop_reason,
            },
        )
        return LLMResponse(message=message, model=model, cost_usd=cost)
