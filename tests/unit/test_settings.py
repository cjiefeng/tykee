from __future__ import annotations

from app.settings import seed_settings, seed_values, set_value
from tests.conftest import Env


async def test_seed_loads_and_does_not_overwrite_edits(env: Env) -> None:
    s = await env.settings.load()
    assert s.model_for("default") == seed_values()["models.default"]
    assert s.model_for("default") in s.pricing
    assert s.persona_system_prompt.startswith("You are Tykee")

    await env.db.write(lambda c: set_value(c, "history.max_turns", 3))
    assert await env.db.write(seed_settings) == 0
    assert (await env.settings.load()).history_max_turns == 3


def test_no_model_ids_in_code() -> None:
    """Model IDs live only in app/seed/settings.json (design §7.0)."""
    from pathlib import Path

    import app

    root = Path(app.__file__).parent
    offenders = [
        str(p.relative_to(root))
        for p in root.rglob("*.py")
        if "claude-" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []
