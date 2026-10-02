"""What approving a non-note inbox suggestion does (§10.4). Kept outside ``MemoryService`` so the
memory layer doesn't depend on the decision engine."""

from __future__ import annotations

import logging

from app.brain.memory import InboxItem, MemoryService
from app.decisions.service import DecisionService
from app.decisions.text import slugify

log = logging.getLogger(__name__)


def register(memory: MemoryService, decisions: DecisionService) -> None:
    async def apply_category(item: InboxItem) -> None:
        p = item.payload
        phrase = str(p.get("phrase", "")).strip()
        if not phrase:
            return
        result = await decisions.resolve(
            phrase=phrase,
            proposed_slug=slugify(phrase) or "decision",
            description=str(p.get("description") or phrase),
            proposed_tau_days=float(p.get("tau_days", 3.0)),
            create_new=True,
        )
        if result.category is not None:
            for alias in p.get("aliases") or []:
                await decisions.resolve(
                    phrase=str(alias),
                    proposed_slug=result.category.slug,
                    description=result.category.description,
                    proposed_tau_days=result.category.recency_tau_days,
                    use_existing=result.category.slug,
                )
        log.info(
            "category created from inbox", extra={"inbox_id": item.id, "status": result.status}
        )

    async def apply_option(item: InboxItem) -> None:
        p = item.payload
        category = await decisions.get_category(int(p.get("category_id", 0)))
        name = str(p.get("name", "")).strip()
        if category is None or not name:
            return
        await decisions.add_option(category, name, [str(t) for t in p.get("tags") or []], "shared")

    memory.appliers["category"] = apply_category
    memory.appliers["option"] = apply_option
