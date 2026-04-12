"""Async PostgreSQL client with connection pooling for Synapto."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import psycopg
from pgvector.psycopg import register_vector_async
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger("synapto.db")


class PostgresClient:
    """Async PostgreSQL client wrapping a connection pool with pgvector support."""

    def __init__(self, dsn: str, min_size: int = 2, max_size: int = 10) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: AsyncConnectionPool | None = None

    async def connect(self) -> None:
        self._pool = AsyncConnectionPool(
            conninfo=self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            open=False,
            kwargs={"row_factory": dict_row},
            configure=self._configure_connection,
        )
        await self._pool.open()
        logger.info("synapto postgres pool opened (min=%d, max=%d)", self._min_size, self._max_size)

    @staticmethod
    async def _configure_connection(conn: psycopg.AsyncConnection) -> None:
        try:
            await register_vector_async(conn)
        except Exception:
            logger.debug("pgvector extension not yet available — will register after migrations")

    async def close(self, timeout: float = 5.0) -> None:
        if self._pool:
            await self._pool.close(timeout=timeout)
            logger.info("synapto postgres pool closed")

    @asynccontextmanager
    async def acquire(self):
        """Acquire a connection from the pool."""
        if not self._pool:
            raise RuntimeError("synapto postgres pool not initialized — call connect() first")
        async with self._pool.connection() as conn:
            yield conn

    async def execute(
        self, query: str, params: tuple | dict | None = None
    ) -> list[dict[str, Any]]:
        async with self.acquire() as conn:
            cursor = await conn.execute(query, params)
            if cursor.description:
                return await cursor.fetchall()
            return []

    async def execute_one(
        self, query: str, params: tuple | dict | None = None
    ) -> dict[str, Any] | None:
        rows = await self.execute(query, params)
        return rows[0] if rows else None

    async def execute_many(
        self, query: str, params_seq: list[tuple | dict]
    ) -> None:
        async with self.acquire() as conn:
            async with conn.cursor() as cur:
                for params in params_seq:
                    await cur.execute(query, params)

    async def execute_returning_id(
        self, query: str, params: tuple | dict | None = None
    ) -> UUID | None:
        row = await self.execute_one(query, params)
        if row and "id" in row:
            return row["id"]
        return None
