"""Unit tests for Synapto database layer."""

from __future__ import annotations

import pytest

from synapto.db.migrations import SCHEMA_VERSION, get_schema_version, run_migrations
from synapto.db.postgres import PostgresClient
from synapto.db.redis_cache import RedisCache

DSN = "postgresql://localhost/synapto"
REDIS_URL = "redis://localhost:6379/1"  # use db 1 for tests


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


class TestPostgresClient:
    async def test_connect_and_query(self, pg: PostgresClient):
        rows = await pg.execute("SELECT 1 AS val;")
        assert rows == [{"val": 1}]

    async def test_execute_one(self, pg: PostgresClient):
        row = await pg.execute_one("SELECT 42 AS answer;")
        assert row == {"answer": 42}

    async def test_execute_one_no_rows(self, pg: PostgresClient):
        row = await pg.execute_one("SELECT 1 WHERE false;")
        assert row is None


class TestMigrations:
    async def test_run_migrations(self, pg: PostgresClient):
        await run_migrations(pg)
        version = await get_schema_version(pg)
        assert version == SCHEMA_VERSION

    async def test_migrations_are_idempotent(self, pg: PostgresClient):
        await run_migrations(pg)
        await run_migrations(pg)
        version = await get_schema_version(pg)
        assert version == SCHEMA_VERSION

    async def test_tables_exist_after_migration(self, pg: PostgresClient):
        await run_migrations(pg)
        for table in ("memories", "entities", "relations", "memory_entities"):
            rows = await pg.execute(
                "SELECT tablename FROM pg_tables WHERE tablename = %s;", (table,)
            )
            assert len(rows) == 1, f"table {table} not found"


class TestRedisCache:
    async def test_cache_and_retrieve_memory(self, cache: RedisCache):
        from uuid import uuid4

        mid = uuid4()
        data = {"content": "test memory", "type": "general"}
        await cache.cache_memory(mid, data)
        result = await cache.get_cached_memory(mid)
        assert result["content"] == "test memory"

    async def test_invalidate_memory(self, cache: RedisCache):
        from uuid import uuid4

        mid = uuid4()
        await cache.cache_memory(mid, {"content": "gone"})
        await cache.invalidate_memory(mid)
        assert await cache.get_cached_memory(mid) is None

    async def test_session_tracking(self, cache: RedisCache):
        from uuid import uuid4

        sid = "test-session"
        m1, m2 = uuid4(), uuid4()
        await cache.add_to_session(sid, m1)
        await cache.add_to_session(sid, m2)
        members = await cache.get_session_memories(sid)
        assert str(m1) in members
        assert str(m2) in members

    async def test_decay_score(self, cache: RedisCache):
        from uuid import uuid4

        mid = uuid4()
        await cache.set_decay_score("default", mid, 0.75)
        score = await cache.get_decay_score("default", mid)
        assert score == 0.75

    async def test_increment_access(self, cache: RedisCache):
        from uuid import uuid4

        mid = uuid4()
        count1 = await cache.increment_access(mid)
        count2 = await cache.increment_access(mid)
        assert count1 == 1
        assert count2 == 2
