"""Repository for HRR memory bank operations.

Design pattern: Repository — isolates memory_banks table SQL behind a domain-oriented API.
"""

from __future__ import annotations

from synapto.db.postgres import PostgresClient

# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------

_UPSERT = """
    INSERT INTO memory_banks (bank_name, vector, dim, fact_count, updated_at)
    VALUES (%s, %s, %s, %s, now())
    ON CONFLICT (bank_name) DO UPDATE SET
        vector = EXCLUDED.vector,
        dim = EXCLUDED.dim,
        fact_count = EXCLUDED.fact_count,
        updated_at = now();
"""

_DELETE = "DELETE FROM memory_banks WHERE bank_name = %s;"

_GET_VECTOR = "SELECT vector FROM memory_banks WHERE bank_name = %s;"

_LIST_TYPES = "SELECT DISTINCT type FROM memories WHERE tenant = %s AND deleted_at IS NULL;"


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class BankRepository:
    """Encapsulates all memory_banks table SQL operations."""

    def __init__(self, client: PostgresClient) -> None:
        self._db = client

    async def upsert(self, bank_name: str, vector: bytes, dim: int, fact_count: int) -> None:
        await self._db.execute(_UPSERT, (bank_name, vector, dim, fact_count))

    async def delete(self, bank_name: str) -> None:
        await self._db.execute(_DELETE, (bank_name,))

    async def get_vector(self, bank_name: str) -> bytes | None:
        row = await self._db.execute_one(_GET_VECTOR, (bank_name,))
        return row["vector"] if row else None

    async def list_tenant_types(self, tenant: str) -> list[str]:
        rows = await self._db.execute(_LIST_TYPES, (tenant,))
        return [row["type"] for row in rows]
