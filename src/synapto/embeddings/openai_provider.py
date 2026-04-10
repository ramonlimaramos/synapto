"""OpenAI embedding provider for Synapto (optional — requires openai package and API key)."""

from __future__ import annotations

import logging
import os

from synapto.embeddings.base import EmbeddingProvider

logger = logging.getLogger("synapto.embeddings.openai")

DEFAULT_MODEL = "text-embedding-3-small"
DEFAULT_DIM = 1536


class OpenAIProvider(EmbeddingProvider):
    """OpenAI embeddings via the openai SDK.

    Requires the `openai` extra: pip install synapto[openai]
    Set OPENAI_API_KEY in your environment.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        dimension: int = DEFAULT_DIM,
    ) -> None:
        self._model = model
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self._dimension = dimension

        if not self._api_key:
            raise ValueError(
                "OpenAI API key required — set OPENAI_API_KEY or pass api_key= to OpenAIProvider"
            )

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def name(self) -> str:
        return f"openai/{self._model}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            from openai import AsyncOpenAI
        except ImportError:
            raise ImportError(
                "openai package not installed — run: pip install synapto[openai]"
            )

        client = AsyncOpenAI(api_key=self._api_key)
        response = await client.embeddings.create(input=texts, model=self._model)
        return [item.embedding for item in response.data]
