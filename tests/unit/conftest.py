"""Shared test fixtures — reads DSN/Redis URL from env vars for CI compatibility."""

from __future__ import annotations

import hashlib
import math
import os

import pytest

from synapto.db.redis_cache import RedisCache
from synapto.embeddings.base import EmbeddingProvider
from tests.db_guard import (
    UnsafeTestDatabaseError,
    decide_test_database_action,
    open_verified_client,
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
    ``tests/db_guard.py`` for the fail-closed rules.
    """
    decision = decide_test_database_action()
    if decision.action == "skip":
        pytest.skip(decision.reason)
    if decision.action == "fail":
        pytest.fail(decision.reason, pytrace=False)

    unsafe_reason = None
    try:
        client = await open_verified_client(decision.dsn)
    except UnsafeTestDatabaseError as exc:
        # open_verified_client already closed the pool; fail outside the except
        # block so the report is the reason rather than an exception chain
        unsafe_reason = str(exc)

    if unsafe_reason is not None:
        pytest.fail(unsafe_reason, pytrace=False)

    try:
        yield client
    finally:
        await client.close()


@pytest.fixture
async def cache():
    c = RedisCache(url=REDIS_URL, prefix="synapto_test")
    await c.connect()
    yield c
    await c.flush_prefix()
    await c.close()
