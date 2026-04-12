"""Shared test fixtures — reads DSN/Redis URL from env vars for CI compatibility."""

from __future__ import annotations

import os

import pytest

from synapto.db.postgres import PostgresClient
from synapto.db.redis_cache import RedisCache

DSN = os.environ.get("SYNAPTO_PG_DSN", "postgresql://localhost/synapto")
REDIS_URL = os.environ.get("SYNAPTO_REDIS_URL", "redis://localhost:6379/1")


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
