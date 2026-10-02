"""Deterministic offline embedder: hashed bag of words, L2-normalised. Texts sharing words get
similar vectors, which is enough to exercise the vector side of hybrid retrieval."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence

DIM = 384
_WORD = re.compile(r"\w+", re.UNICODE)


def fake_vector(text: str) -> list[float]:
    v = [0.0] * DIM
    for tok in _WORD.findall(text.casefold()):
        h = int(hashlib.sha1(tok.encode()).hexdigest(), 16)
        v[h % DIM] += 1.0 if (h >> 9) & 1 else -1.0
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    if norm == 1.0 and not any(v):
        v[0] = 1.0
    return [x / norm for x in v]


class FakeEmbedder:
    def __init__(self, model_id: str = "fake-embedder@test") -> None:
        self._model_id = model_id
        self.fail = False
        self.passage_calls: list[list[str]] = []
        self.query_calls: list[str] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    async def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        if self.fail:
            raise RuntimeError("embedder down")
        self.passage_calls.append(list(texts))
        return [fake_vector(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        if self.fail:
            raise RuntimeError("embedder down")
        self.query_calls.append(text)
        return fake_vector(text)
