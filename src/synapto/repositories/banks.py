"""Repository for HRR memory bank operations.

Design pattern: Repository — isolates memory_banks table SQL behind a domain-oriented API.
"""

from __future__ import annotations

from synapto.db.postgres import PostgresClient
from synapto.sql import banks as sql


class BankRepository:
    """Encapsulates all memory_banks table SQL operations."""

    def __init__(self, client: PostgresClient) -> None:
        self._db = client

    async def upsert(self, bank_name: str, vector: bytes, dim: int, fact_count: int) -> None:
        await self._db.execute(sql.UPSERT, (bank_name, vector, dim, fact_count))

    async def delete(self, bank_name: str) -> None:
        await self._db.execute(sql.DELETE, (bank_name,))

    async def get_vector(self, bank_name: str) -> bytes | None:
        row = await self._db.execute_one(sql.GET_VECTOR, (bank_name,))
        return row["vector"] if row else None

    async def list_tenant_types(self, tenant: str) -> list[str]:
        rows = await self._db.execute(sql.LIST_TYPES, (tenant,))
        return [row["type"] for row in rows]
