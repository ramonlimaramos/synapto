"""Abstract embedding provider interface for Synapto."""

from __future__ import annotations

from abc import ABC, abstractmethod


class EmbeddingProvider(ABC):
    """Base class for all embedding providers."""

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Return the embedding vector dimension."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the provider name."""

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for a list of texts.

        Returns a list of float vectors, one per input text.
        """

    async def embed_one(self, text: str) -> list[float]:
        """Generate a single embedding. Convenience wrapper around embed()."""
        results = await self.embed([text])
        return results[0]
