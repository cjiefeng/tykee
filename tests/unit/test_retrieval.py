"""Hybrid retrieval with owner filtering (§6.5)."""

from __future__ import annotations

import pytest

from app.brain.retrieval import Retriever, fts_query, rrf
from tests.conftest import Env, make_stack


async def _seed(env: Env) -> Retriever:
    stack = make_stack(env)
    w = stack.store.write
    await w(
        "people/jack",
        mode="create",
        title="Jack",
        content="Allergic to peanuts. Loves spicy laksa.",
    )
    await w(
        "people/partner",
        mode="create",
        title="Partner",
        content="Vegetarian on Mondays. Loves durian.",
    )
    await w("memories/jack/coffee", mode="create", content="Drinks kopi-o kosong every morning.")
    await w(
        "memories/partner/secret-gift",
        mode="create",
        content="Planning a surprise laksa cooking class.",
    )
    await w(
        "shared/places/ah-hock-laksa",
        mode="create",
        content="Katong laksa stall. See [[shared/topics/katong]].",
    )
    await w("shared/topics/katong", mode="create", content="Katong neighbourhood food walk notes.")
    await w(
        "shared/topics/movies", mode="create", content="We like sci-fi films and slow thrillers."
    )
    return Retriever(env.db, stack.embedder)


async def test_owner_filter_hides_other_users_notes(env: Env) -> None:
    r = await _seed(env)
    jack_only = {h.path for h in await r.search("laksa", ["jack", "shared"], k=10)}
    assert "memories/partner/secret-gift.md" not in jack_only
    assert {"people/jack.md", "shared/places/ah-hock-laksa.md"} <= jack_only
    both = {h.path for h in await r.search("laksa", ["jack", "partner", "shared"], k=10)}
    assert "memories/partner/secret-gift.md" in both
    shared_only = await r.search("laksa", ["shared"], k=10)
    assert {h.owner for h in shared_only} == {"shared"}


async def test_keyword_and_vector_hits_are_fused(env: Env) -> None:
    r = await _seed(env)
    hits = await r.search("kopi coffee morning", ["jack", "shared"], k=3)
    assert hits[0].path == "memories/jack/coffee.md" and hits[0].via == "match"
    assert hits[0].snippet == "Drinks kopi-o kosong every morning."


async def test_link_neighbours_added_below_matches(env: Env) -> None:
    r = await _seed(env)
    hits = await r.search("Katong laksa stall", ["shared"], k=1)
    assert [h.path for h in hits] == ["shared/places/ah-hock-laksa.md", "shared/topics/katong.md"]
    assert hits[1].via == "link" and hits[1].score < hits[0].score


async def test_neighbours_are_owner_filtered(env: Env) -> None:
    stack = make_stack(env)
    await stack.store.write(
        "shared/topics/plans",
        mode="create",
        content="Anniversary ideas [[memories/partner/secret-gift]]",
    )
    await stack.store.write("memories/partner/secret-gift", mode="create", content="cooking class")
    hits = await Retriever(env.db, stack.embedder).search(
        "anniversary ideas", ["jack", "shared"], k=5
    )
    assert [h.path for h in hits] == ["shared/topics/plans.md"]


async def test_embedding_failure_falls_back_to_keywords(env: Env) -> None:
    r = await _seed(env)
    stack_emb = r._embedder
    stack_emb.fail = True  # type: ignore[attr-defined]
    hits = await r.search("durian", ["partner", "shared"], k=3)
    assert hits and hits[0].path == "people/partner.md"


async def test_degenerate_queries_are_safe(env: Env) -> None:
    r = await _seed(env)
    await r.search("?! ()*", ["jack", "shared"], k=3)  # no FTS terms: vector side only, no error
    assert await r.search("laksa", [], k=3) == []  # no allowed owners → nothing


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("what to eat tonight", '"what" OR "to" OR "eat" OR "tonight"'),
        ('NEAR(a b) OR "xy" -yz *', '"near" OR "or" OR "xy" OR "yz"'),
        ("想吃 辣的", '"想吃" OR "辣的"'),
        ("?!", None),
    ],
)
def test_fts_query_is_injection_safe(text: str, expected: str | None) -> None:
    assert fts_query(text) == expected


def test_rrf_math() -> None:
    scores = rrf([[1, 2, 3], [3, 1]])
    assert scores[1] == pytest.approx(1 / 61 + 1 / 62)
    assert scores[3] == pytest.approx(1 / 63 + 1 / 61)
    assert scores[2] == pytest.approx(1 / 62)
    assert sorted(scores, key=lambda k: -scores[k]) == [1, 3, 2]
