"""Memory bank management for HRR-based category indexing.

A memory bank is a bundled superposition of all HRR vectors within a category
(tenant + depth_layer or type). This enables O(1) category-level queries instead
of scanning individual memories.

Design pattern: Facade -- provides a simplified interface for building and querying
composite HRR banks from the underlying PostgreSQL storage.
"""

from __future__ import annotations

import logging

from synapto.db.postgres import PostgresClient
from synapto.hrr.core import DEFAULT_DIM, bundle, bytes_to_phases, phases_to_bytes, snr_estimate

logger = logging.getLogger("synapto.hrr.banks")


async def rebuild_bank(
    client: PostgresClient,
    bank_name: str,
    tenant: str,
    dim: int = DEFAULT_DIM,
    type_filter: str | None = None,
    depth_filter: str | None = None,
) -> int:
    """Rebuild a memory bank by bundling all matching HRR vectors.

    Returns the number of vectors bundled. Deletes the bank if no vectors found.
    """
    where = ["deleted_at IS NULL", "tenant = %s", "hrr_vector IS NOT NULL"]
    params: list = [tenant]

    if type_filter:
        where.append("type = %s")
        params.append(type_filter)
    if depth_filter:
        where.append("depth_layer = %s")
        params.append(depth_filter)

    rows = await client.execute(
        f"SELECT hrr_vector FROM memories WHERE {' AND '.join(where)};",
        tuple(params),
    )

    if not rows:
        await client.execute(
            "DELETE FROM memory_banks WHERE bank_name = %s;", (bank_name,)
        )
        return 0

    vectors = [bytes_to_phases(row["hrr_vector"]) for row in rows]
    bank_vector = bundle(*vectors)
    fact_count = len(vectors)

    snr_estimate(dim, fact_count)

    await client.execute(
        """
        INSERT INTO memory_banks (bank_name, vector, dim, fact_count, updated_at)
        VALUES (%s, %s, %s, %s, now())
        ON CONFLICT (bank_name) DO UPDATE SET
            vector = EXCLUDED.vector,
            dim = EXCLUDED.dim,
            fact_count = EXCLUDED.fact_count,
            updated_at = now();
        """,
        (bank_name, phases_to_bytes(bank_vector), dim, fact_count),
    )

    logger.info("rebuilt bank '%s': %d vectors, dim=%d", bank_name, fact_count, dim)
    return fact_count


async def rebuild_tenant_banks(
    client: PostgresClient,
    tenant: str,
    dim: int = DEFAULT_DIM,
) -> int:
    """Rebuild all banks for a tenant (one per type)."""
    rows = await client.execute(
        "SELECT DISTINCT type FROM memories WHERE tenant = %s AND deleted_at IS NULL;",
        (tenant,),
    )
    total = 0
    for row in rows:
        bank_name = f"{tenant}:{row['type']}"
        count = await rebuild_bank(client, bank_name, tenant, dim, type_filter=row["type"])
        total += count
    return total


async def get_bank_vector(client: PostgresClient, bank_name: str) -> bytes | None:
    """Retrieve a memory bank's bundled vector."""
    row = await client.execute_one(
        "SELECT vector FROM memory_banks WHERE bank_name = %s;", (bank_name,)
    )
    return row["vector"] if row else None
