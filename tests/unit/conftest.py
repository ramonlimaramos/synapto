"""Shared test fixtures — reads DSN/Redis URL from env vars for CI compatibility."""

from __future__ import annotations

import hashlib
import math
import os

import pytest

from synapto.db.postgres import PostgresClient
from synapto.db.redis_cache import RedisCache
from synapto.embeddings.base import EmbeddingProvider

DSN = os.environ.get("SYNAPTO_PG_DSN", "postgresql://localhost/synapto")
REDIS_URL = os.environ.get("SYNAPTO_REDIS_URL", "redis://localhost:6379/1")
TEST_EMBEDDING_DIM = 384


class DeterministicEmbeddingProvider(EmbeddingProvider):
    """Offline embedding provider for unit tests."""

    @property
    def dimension(self) -> int:
        return TEST_EMBEDDING_DIM

    @property
    def name(self) -> str:
        return "test/deterministic"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [_embed_text(text) for text in texts]


def _embed_text(text: str) -> list[float]:
    tokens = _normalize_tokens(text)
    vector = [0.0] * TEST_EMBEDDING_DIM
    for token in tokens:
        digest = hashlib.blake2b(token.encode(), digest_size=4).digest()
        index = int.from_bytes(digest, "big") % TEST_EMBEDDING_DIM
        vector[index] += 1.0

    magnitude = math.sqrt(sum(value * value for value in vector))
    if magnitude == 0:
        vector[0] = 1.0
        return vector
    return [value / magnitude for value in vector]


def _normalize_tokens(text: str) -> list[str]:
    raw_tokens = [
        token.strip(".,;:!?()[]{}'\"`").lower()
        for token in text.replace("-", " ").split()
    ]
    tokens = []
    for token in raw_tokens:
        if not token:
            continue
        tokens.append(token)
        if token.endswith("s") and len(token) > 3:
            tokens.append(token[:-1])
        if token == "pgvector":
            tokens.extend(["vector", "database"])
        if token in {"postgresql", "redis"}:
            tokens.append("database")
        if token == "kafka":
            tokens.extend(["message", "queue", "streaming"])
    return tokens


@pytest.fixture(scope="session")
def provider():
    """Provide deterministic offline embeddings for unit tests."""
    return DeterministicEmbeddingProvider()


@pytest.fixture
async def pg():
    client = PostgresClient(DSN, min_size=1, max_size=2)
    await client.connect()
    yield client
    await client.close()


@pytest.fixture
async def cache():
    c = RedisCache(url=REDIS_URL, prefix="synapto_test")
    await c.connect()
    yield c
    await c.flush_prefix()
    await c.close()
