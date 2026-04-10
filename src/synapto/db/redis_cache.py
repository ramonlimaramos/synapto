"""Redis hot cache layer for Synapto — caches recent memories and session context."""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

import redis.asyncio as aioredis

logger = logging.getLogger("synapto.db.redis")

DEFAULT_TTL = 86400  # 24 hours
SESSION_TTL = 3600  # 1 hour


class RedisCache:
    """Async Redis cache for hot memories and session tracking."""

    def __init__(self, url: str = "redis://localhost:6379/0", prefix: str = "synapto") -> None:
        self._url = url
        self._prefix = prefix
        self._client: aioredis.Redis | None = None

    async def connect(self) -> None:
        self._client = aioredis.from_url(self._url, decode_responses=True)
        await self._client.ping()
        logger.info("synapto redis connected: %s", self._url)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            logger.info("synapto redis closed")

    def _key(self, *parts: str) -> str:
        return f"{self._prefix}:{':'.join(parts)}"

    # --- memory cache ---

    async def cache_memory(self, memory_id: UUID, data: dict[str, Any], ttl: int = DEFAULT_TTL) -> None:
        key = self._key("mem", str(memory_id))
        await self._client.set(key, json.dumps(data, default=str), ex=ttl)

    async def get_cached_memory(self, memory_id: UUID) -> dict[str, Any] | None:
        key = self._key("mem", str(memory_id))
        raw = await self._client.get(key)
        if raw:
            return json.loads(raw)
        return None

    async def invalidate_memory(self, memory_id: UUID) -> None:
        key = self._key("mem", str(memory_id))
        await self._client.delete(key)

    # --- session context ---

    async def add_to_session(self, session_id: str, memory_id: UUID) -> None:
        key = self._key("session", session_id)
        await self._client.sadd(key, str(memory_id))
        await self._client.expire(key, SESSION_TTL)

    async def get_session_memories(self, session_id: str) -> set[str]:
        key = self._key("session", session_id)
        return await self._client.smembers(key)

    async def clear_session(self, session_id: str) -> None:
        key = self._key("session", session_id)
        await self._client.delete(key)

    # --- decay scores ---

    async def set_decay_score(self, tenant: str, memory_id: UUID, score: float) -> None:
        key = self._key("decay", tenant)
        await self._client.hset(key, str(memory_id), str(score))

    async def get_decay_score(self, tenant: str, memory_id: UUID) -> float | None:
        key = self._key("decay", tenant)
        val = await self._client.hget(key, str(memory_id))
        return float(val) if val else None

    # --- stats ---

    async def increment_access(self, memory_id: UUID) -> int:
        key = self._key("access", str(memory_id))
        count = await self._client.incr(key)
        await self._client.expire(key, DEFAULT_TTL)
        return count

    # --- bulk operations ---

    async def flush_prefix(self) -> int:
        """Delete all keys with the synapto prefix. Use with caution."""
        cursor = "0"
        deleted = 0
        while cursor:
            cursor, keys = await self._client.scan(cursor=cursor, match=f"{self._prefix}:*", count=100)
            if keys:
                await self._client.delete(*keys)
                deleted += len(keys)
            if cursor == 0:
                break
        return deleted
