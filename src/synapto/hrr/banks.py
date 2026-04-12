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
from synapto.repositories.banks import BankRepository
from synapto.repositories.memories import MemoryRepository

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
    mem_repo = MemoryRepository(client)
    bank_repo = BankRepository(client)

    rows = await mem_repo.select_hrr_vectors(tenant, type_filter, depth_filter)

    if not rows:
        await bank_repo.delete(bank_name)
        return 0

    vectors = [bytes_to_phases(row["hrr_vector"]) for row in rows]
    bank_vector = bundle(*vectors)
    fact_count = len(vectors)

    snr_estimate(dim, fact_count)

    await bank_repo.upsert(bank_name, phases_to_bytes(bank_vector), dim, fact_count)

    logger.info("rebuilt bank '%s': %d vectors, dim=%d", bank_name, fact_count, dim)
    return fact_count


async def rebuild_tenant_banks(
    client: PostgresClient,
    tenant: str,
    dim: int = DEFAULT_DIM,
) -> int:
    """Rebuild all banks for a tenant (one per type)."""
    bank_repo = BankRepository(client)
    types = await bank_repo.list_tenant_types(tenant)
    total = 0
    for mem_type in types:
        bank_name = f"{tenant}:{mem_type}"
        count = await rebuild_bank(client, bank_name, tenant, dim, type_filter=mem_type)
        total += count
    return total


async def get_bank_vector(client: PostgresClient, bank_name: str) -> bytes | None:
    """Retrieve a memory bank's bundled vector."""
    return await BankRepository(client).get_vector(bank_name)
