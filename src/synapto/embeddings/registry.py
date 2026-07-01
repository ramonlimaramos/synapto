"""Embedding provider registry — auto-selects the best available provider."""

from __future__ import annotations

import logging
import os

from synapto.embeddings.base import EmbeddingProvider

logger = logging.getLogger("synapto.embeddings.registry")

_PROVIDERS: dict[str, type[EmbeddingProvider]] = {}


def register(name: str, cls: type[EmbeddingProvider]) -> None:
    _PROVIDERS[name] = cls


def _openai_kwargs(kwargs: dict) -> dict:
    """Translate generic embedding config kwargs to the OpenAI provider shape."""
    provider_kwargs = {k: v for k, v in kwargs.items() if k != "device"}
    if "model_name" in provider_kwargs and "model" not in provider_kwargs:
        provider_kwargs["model"] = provider_kwargs.pop("model_name")
    return provider_kwargs


def get_provider(name: str | None = None, **kwargs) -> EmbeddingProvider:
    """Get an embedding provider by name, or auto-select the best available.

    Priority when name is None:
    1. OpenAI (if OPENAI_API_KEY is set and openai is installed)
    2. SentenceTransformers (always available, local, free)
    """
    if name:
        if name in _PROVIDERS:
            return _PROVIDERS[name](**kwargs)

        if name.startswith("openai"):
            from synapto.embeddings.openai_provider import OpenAIProvider
            return OpenAIProvider(**_openai_kwargs(kwargs))

        if name.startswith("sentence-transformer"):
            from synapto.embeddings.sentence_transformer import SentenceTransformerProvider
            return SentenceTransformerProvider(**kwargs)

        raise ValueError(f"unknown embedding provider: {name}")

    # auto-select
    if os.environ.get("OPENAI_API_KEY"):
        try:
            from synapto.embeddings.openai_provider import OpenAIProvider
            provider = OpenAIProvider(**_openai_kwargs(kwargs))
            logger.info("auto-selected provider: %s", provider.name)
            return provider
        except (ImportError, ValueError):
            pass

    from synapto.embeddings.sentence_transformer import SentenceTransformerProvider
    provider = SentenceTransformerProvider(**kwargs)
    logger.info("auto-selected provider: %s", provider.name)
    return provider


def list_providers() -> list[str]:
    """List all registered provider names plus built-in ones."""
    builtin = ["sentence-transformers", "openai"]
    return list(set(builtin + list(_PROVIDERS.keys())))
