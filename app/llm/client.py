"""The only code allowed to call the Claude API (§7.0).

Every call: resolve model from settings → budget check → Messages API call (timeout, SDK
exponential-backoff retries on 408/409/429/5xx/overloaded) → ``usage`` row with computed cost.
The bootstrap import (§15) also submits Message Batches here; their usage rows are written when
results are collected, at the batch discount.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast
from zoneinfo import ZoneInfo

import anthropic
from anthropic.types import (
    Message,
    MessageParam,
    TextBlockParam,
    ToolChoiceParam,
    ToolParam,
)
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from app.db.database import Database
from app.db.repos import usage as usage_repo
from app.health import HealthState
from app.llm.pricing import cost_usd
from app.settings import ModelRole, RuntimeSettings, SettingsStore
from app.timeutil import local_day_start, local_month_start, to_sql, utcnow

log = logging.getLogger(__name__)

Purpose = Literal["chat", "judge", "summary", "import_extract", "import_consolidate", "harvest"]

INTERACTIVE_TIMEOUT_S = 30.0
CONSOLIDATION_TIMEOUT_S = 600.0
INTERACTIVE_MAX_RETRIES = 2
BATCH_DISCOUNT = 0.5  # Message Batches bill every token at half price


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
    tool_choice: ToolChoiceParam | None = None
    json_schema: dict[str, Any] | None = None  # structured output (output_config.format)
    user_id: int | None = None
    chat_id: int | None = None
    import_job_id: int | None = None
    timeout_s: float = INTERACTIVE_TIMEOUT_S
    max_retries: int = INTERACTIVE_MAX_RETRIES
    stream: bool = False  # long outputs (import consolidation): stream to avoid idle timeouts


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


BatchStatus = Literal["in_progress", "canceling", "ended"]


@dataclass(frozen=True)
class BatchItemResult:
    custom_id: str
    response: LLMResponse | None  # None → ``error`` says why (errored / canceled / expired)
    error: str = ""


class BatchClient(Protocol):
    """Message Batches (§15.3 EXTRACT). Every request in a batch uses the same purpose/model."""

    async def submit_batch(self, items: Sequence[tuple[str, LLMRequest]]) -> str: ...

    async def batch_status(self, batch_id: str) -> BatchStatus: ...

    async def batch_results(self, batch_id: str, template: LLMRequest) -> list[BatchItemResult]: ...

    async def cancel_batch(self, batch_id: str) -> None: ...


@dataclass(frozen=True)
class BudgetStatus:
    daily_spent: float
    monthly_spent: float
    daily_cap: float
    monthly_cap: float


async def budget_status(db: Database, settings: RuntimeSettings, tz: ZoneInfo) -> BudgetStatus:
    """The import (§15) counts against the monthly cap only: a one-off $3-10 job must not put
    the chat into fallback mode for the rest of the day (§13)."""
    now = utcnow()
    day_since = to_sql(local_day_start(now, tz))
    month_since = to_sql(local_month_start(now, tz))

    def _q(conn: sqlite3.Connection) -> tuple[float, float]:
        return (
            usage_repo.cost_since(conn, day_since, include_import=False),
            usage_repo.cost_since(conn, month_since),
        )

    daily, monthly = await db.read(_q)
    return BudgetStatus(daily, monthly, settings.budget_daily_usd, settings.budget_monthly_usd)


def check_budget(status: BudgetStatus, *, monthly_only: bool = False) -> None:
    if status.monthly_spent >= status.monthly_cap:
        raise BudgetExceeded("monthly", status.monthly_spent, status.monthly_cap)
    if not monthly_only and status.daily_spent >= status.daily_cap:
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
        health: HealthState | None = None,
    ) -> None:
        self._health = health
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

    def _record(self, *, ok: bool) -> None:
        if self._health is not None:
            self._health.llm_result(ok)

    async def complete(self, req: LLMRequest) -> LLMResponse:
        client = self._require()
        settings = await self._settings.load()
        await self._check_budget(settings, req)
        model = settings.model_for(req.model_role)
        sdk = client.with_options(timeout=req.timeout_s, max_retries=req.max_retries)
        params = self._params(req, model, settings)
        try:
            if req.stream:
                async with sdk.messages.stream(**params) as stream:
                    message = await stream.get_final_message()
            else:
                message = await sdk.messages.create(**params)
        except anthropic.APIError as e:
            raise self._failed(e, req, model) from e
        self._record(ok=True)
        cost = await self._record_usage(req, model, message, settings)
        return LLMResponse(message=message, model=model, cost_usd=cost)

    # --- Message Batches (§15.3) -----------------------------------------------------------------

    async def submit_batch(self, items: Sequence[tuple[str, LLMRequest]]) -> str:
        client = self._require()
        if not items:
            raise ValueError("empty batch")
        settings = await self._settings.load()
        await self._check_budget(settings, items[0][1])
        requests: list[Request] = []
        for custom_id, req in items:
            model = settings.model_for(req.model_role)
            params = cast(MessageCreateParamsNonStreaming, self._params(req, model, settings))
            requests.append(Request(custom_id=custom_id, params=params))
        sdk = client.with_options(timeout=120.0, max_retries=INTERACTIVE_MAX_RETRIES)
        try:
            batch = await sdk.messages.batches.create(requests=requests)
        except anthropic.APIError as e:
            raise self._failed(e, items[0][1], "batch") from e
        log.info("batch submitted", extra={"batch_id": batch.id, "requests": len(requests)})
        return batch.id

    async def batch_status(self, batch_id: str) -> BatchStatus:
        client = self._require()
        try:
            batch = await client.with_options(timeout=60.0).messages.batches.retrieve(batch_id)
        except anthropic.APIError as e:
            raise LLMUnavailable(type(e).__name__) from e
        return batch.processing_status

    async def batch_results(self, batch_id: str, template: LLMRequest) -> list[BatchItemResult]:
        """Collect an ended batch. Writes one ``usage`` row per succeeded request, at the batch
        discount, tagged with ``template``'s purpose and import job."""
        client = self._require()
        settings = await self._settings.load()
        out: list[BatchItemResult] = []
        try:
            decoder = await client.with_options(timeout=300.0).messages.batches.results(batch_id)
            async for item in decoder:
                result = item.result
                if result.type != "succeeded":
                    detail = result.error.error.type if result.type == "errored" else ""
                    out.append(BatchItemResult(item.custom_id, None, f"{result.type} {detail}"))
                    continue
                msg = result.message
                cost = await self._record_usage(
                    template, msg.model, msg, settings, discount=BATCH_DISCOUNT
                )
                out.append(BatchItemResult(item.custom_id, LLMResponse(msg, msg.model, cost)))
        except anthropic.APIError as e:
            raise LLMUnavailable(type(e).__name__) from e
        return out

    async def cancel_batch(self, batch_id: str) -> None:
        client = self._require()
        try:
            await client.with_options(timeout=60.0).messages.batches.cancel(batch_id)
        except anthropic.APIError as e:
            log.warning("batch cancel failed", extra={"batch_id": batch_id, "error": str(e)})

    # --- helpers -----------------------------------------------------------------------------

    def _require(self) -> anthropic.AsyncAnthropic:
        if self._client is None or self.auth_failed:
            raise LLMUnavailable("ANTHROPIC_API_KEY missing or rejected")
        return self._client

    async def _check_budget(self, settings: RuntimeSettings, req: LLMRequest) -> None:
        status = await budget_status(self._db, settings, self._tz)
        check_budget(status, monthly_only=req.import_job_id is not None)

    @staticmethod
    def _params(req: LLMRequest, model: str, settings: RuntimeSettings) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": req.max_tokens or settings.llm_max_tokens,
            "system": list(req.system),
            "messages": list(req.messages),
        }
        if req.tools:
            params["tools"] = list(req.tools)
        if req.tool_choice is not None:
            params["tool_choice"] = req.tool_choice
        if req.json_schema is not None:
            params["output_config"] = {"format": {"type": "json_schema", "schema": req.json_schema}}
        return params

    def _failed(self, e: anthropic.APIError, req: LLMRequest, model: str) -> LLMUnavailable:
        self._record(ok=False)
        if isinstance(e, anthropic.AuthenticationError):
            self.auth_failed = True
            log.error("anthropic auth failed; switching to fallback mode")
            return LLMUnavailable("API key rejected")
        log.warning(
            "anthropic call failed",
            extra={"purpose": req.purpose, "model": model, "error": type(e).__name__},
        )
        return LLMUnavailable(type(e).__name__)

    async def _record_usage(
        self,
        req: LLMRequest,
        model: str,
        message: Message,
        settings: RuntimeSettings,
        discount: float = 1.0,
    ) -> float:
        u = message.usage
        cache_read = u.cache_read_input_tokens or 0
        cache_write = u.cache_creation_input_tokens or 0
        price = settings.pricing.get(model)
        if price is None:
            log.warning("no pricing for model; cost recorded as 0", extra={"model": model})
        cost = discount * cost_usd(
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
        return cost
