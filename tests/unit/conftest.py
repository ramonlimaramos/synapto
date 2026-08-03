"""Shared test fixtures — reads DSN/Redis URL from env vars for CI compatibility."""

from __future__ import annotations

import hashlib
import math
import os

import pytest

from synapto.db.postgres import PostgresClient
from synapto.db.redis_cache import RedisCache
from synapto.embeddings.base import EmbeddingProvider
from tests.db_guard import (
    TEST_DSN_ENV,
    UnsafeTestDatabaseError,
    resolve_test_dsn,
    verify_disposable_database,
)

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
    """Connect to a disposable test database, or skip.

    This suite is destructive — it rolls migrations down and truncates tables —
    so it never guesses a DSN and never touches the production one. See
    ``tests/db_guard.py`` for the two fail-closed rules.
    """
    dsn = resolve_test_dsn()
    if dsn is None:
        pytest.skip(
            f"{TEST_DSN_ENV} is not set — export it pointing at a disposable "
            "*_test database to run the PostgreSQL-backed tests "
            "(see 'Running the tests' in README.md)"
        )

    client = PostgresClient(dsn, min_size=1, max_size=2)
    await client.connect()

    unsafe_reason = None
    try:
        await verify_disposable_database(client)
    except UnsafeTestDatabaseError as exc:
        unsafe_reason = str(exc)

    if unsafe_reason is not None:
        # close before failing so a rejected run does not also leak the pool, and
        # fail outside the except block so the report is the reason, not a chain
        await client.close()
        pytest.fail(unsafe_reason, pytrace=False)

    yield client
    await client.close()


@pytest.fixture
async def cache():
    c = RedisCache(url=REDIS_URL, prefix="synapto_test")
    await c.connect()
    yield c
    await c.flush_prefix()
    await c.close()
