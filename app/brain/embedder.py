"""Local embeddings (§6.8, §6.9): fastembed + multilingual-e5-small, int8 or fp32 weights.

e5 needs ``"query: "`` on search inputs and ``"passage: "`` on indexed text; that's applied
here so callers can't forget it. Inference runs on one dedicated thread, never on the event
loop, and the ONNX session is capped at two threads to stay inside the container budget.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal, Protocol

log = logging.getLogger(__name__)

MODEL_NAME = "intfloat/multilingual-e5-small"
DIM = 384
Precision = Literal["int8", "fp32"]

# Hugging Face sources per precision. int8 is Xenova's quantized export of the same model.
_SOURCES: dict[Precision, tuple[str, str]] = {
    "int8": ("Xenova/multilingual-e5-small", "onnx/model_quantized.onnx"),
    "fp32": ("intfloat/multilingual-e5-small", "onnx/model.onnx"),
}

Vector = list[float]


def model_id(precision: Precision) -> str:
    """What ``chunks.embed_model`` records; any change triggers a full reindex (§6.3)."""
    return f"{MODEL_NAME}@{precision}"


class Embedder(Protocol):
    @property
    def model_id(self) -> str: ...

    async def embed_passages(self, texts: Sequence[str]) -> list[Vector]: ...

    async def embed_query(self, text: str) -> Vector: ...


def _fastembed_name(precision: Precision) -> str:
    return f"tykee/multilingual-e5-small-{precision}"


def _register(precision: Precision) -> str:
    from fastembed import TextEmbedding
    from fastembed.common.model_description import ModelSource, PoolingType

    name = _fastembed_name(precision)
    known = {m["model"] for m in TextEmbedding.list_supported_models()}
    if name not in known:
        repo, model_file = _SOURCES[precision]
        TextEmbedding.add_custom_model(
            model=name,
            pooling=PoolingType.MEAN,
            normalization=True,
            sources=ModelSource(hf=repo),
            dim=DIM,
            model_file=model_file,
        )
    return name


def cache_dir_for(precision: Precision, baked_dir: Path, data_dir: Path) -> Path:
    """Use the copy baked into the image if it's the configured variant; otherwise download
    into ``/data/models`` (persisted, writable)."""
    repo, _ = _SOURCES[precision]
    marker = baked_dir / f"models--{repo.replace('/', '--')}"
    return baked_dir if marker.exists() else data_dir / "models"


def download(precision: Precision, cache_dir: Path) -> None:
    """Fetch the model files (used at image build time)."""
    from fastembed import TextEmbedding

    TextEmbedding(_register(precision), cache_dir=str(cache_dir), threads=1)


class FastEmbedder:
    def __init__(self, precision: Precision, cache_dir: Path, threads: int = 2) -> None:
        self._precision = precision
        self._cache_dir = cache_dir
        self._threads = threads
        self._model: Any = None
        self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="embedder")

    @property
    def model_id(self) -> str:
        return model_id(self._precision)

    def _load(self) -> Any:
        if self._model is None:
            from fastembed import TextEmbedding

            os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
            self._model = TextEmbedding(
                _register(self._precision), cache_dir=str(self._cache_dir), threads=self._threads
            )
            log.info("embedding model loaded", extra={"model": self.model_id})
        return self._model

    def _embed(self, texts: list[str]) -> list[Vector]:
        model = self._load()
        return [vec.tolist() for vec in model.embed(texts, batch_size=16)]

    async def warm_up(self) -> None:
        await asyncio.get_running_loop().run_in_executor(self._exec, self._load)

    async def embed_passages(self, texts: Sequence[str]) -> list[Vector]:
        if not texts:
            return []
        prefixed = [f"passage: {t}" for t in texts]
        return await asyncio.get_running_loop().run_in_executor(self._exec, self._embed, prefixed)

    async def embed_query(self, text: str) -> Vector:
        vecs = await asyncio.get_running_loop().run_in_executor(
            self._exec, self._embed, [f"query: {text}"]
        )
        return vecs[0]

    def close(self) -> None:
        self._exec.shutdown(wait=False, cancel_futures=True)
