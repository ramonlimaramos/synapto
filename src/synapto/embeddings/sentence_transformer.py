"""Local sentence-transformers embedding provider for Synapto."""

from __future__ import annotations

import asyncio
import logging
import os
from functools import lru_cache

from synapto.embeddings.base import EmbeddingProvider

logger = logging.getLogger("synapto.embeddings.st")

DEFAULT_MODEL = "multi-qa-MiniLM-L6-cos-v1"
DEFAULT_DIM = 384


@lru_cache(maxsize=4)
def _load_model(model_name: str, device: str | None):
    """Load the sentence-transformers model (cached singleton)."""
    from sentence_transformers import SentenceTransformer

    logger.info("loading sentence-transformers model: %s (device=%s)", model_name, device or "auto")
    if device:
        return SentenceTransformer(model_name, device=device)
    return SentenceTransformer(model_name)


class SentenceTransformerProvider(EmbeddingProvider):
    """Local embedding provider using sentence-transformers.

    Runs on CPU, no API key required. Default model: multi-qa-MiniLM-L6-cos-v1 (384 dim).
    """

    def __init__(self, model_name: str = DEFAULT_MODEL, device: str | None = None) -> None:
        self._model_name = model_name
        self._device = device or os.environ.get("SYNAPTO_EMBEDDING_DEVICE") or None
        self._dim: int | None = None

    @property
    def device(self) -> str | None:
        return self._device

    @property
    def dimension(self) -> int:
        if self._dim is None:
            model = _load_model(self._model_name, self._device)
            self._dim = model.get_embedding_dimension()
        return self._dim

    @property
    def name(self) -> str:
        return f"sentence-transformers/{self._model_name}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        model = _load_model(self._model_name, self._device)
        loop = asyncio.get_running_loop()
        embeddings = await loop.run_in_executor(
            None,
            lambda: model.encode(
                texts,
                normalize_embeddings=True,
                show_progress_bar=False,
            ),
        )
        return [vec.tolist() for vec in embeddings]
