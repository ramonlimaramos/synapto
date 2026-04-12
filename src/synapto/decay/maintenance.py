"""Decay maintenance — periodic score updates and ephemeral memory cleanup."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from synapto.db.postgres import PostgresClient
from synapto.decay.scoring import calculate_decay_score
from synapto.repositories.memories import MemoryRepository

logger = logging.getLogger("synapto.decay.maintenance")


async def update_decay_scores(client: PostgresClient, batch_size: int = 500) -> int:
    """Recalculate decay scores for all active memories. Returns count of updated rows."""
    repo = MemoryRepository(client)
    rows = await repo.select_for_decay(batch_size)

    if not rows:
        return 0

    now = datetime.now(UTC)
    updates = []
    for row in rows:
        score = calculate_decay_score(
            depth_layer=row["depth_layer"],
            created_at=row["created_at"],
            accessed_at=row["accessed_at"],
            access_count=row["access_count"],
            now=now,
        )
        updates.append((score, row["id"]))

    await repo.update_decay_scores(updates)

    logger.info("updated decay scores for %d memories", len(updates))
    return len(updates)


async def cleanup_ephemeral(client: PostgresClient, max_age_hours: int = 24) -> int:
    """Soft-delete ephemeral memories older than max_age_hours."""
    repo = MemoryRepository(client)
    rows = await repo.cleanup_ephemeral(max_age_hours)
    count = len(rows)
    if count > 0:
        logger.info("soft-deleted %d stale ephemeral memories", count)
    return count


async def purge_deleted(client: PostgresClient, older_than_days: int = 30) -> int:
    """Permanently delete soft-deleted memories older than N days."""
    repo = MemoryRepository(client)
    rows = await repo.purge_deleted(older_than_days)
    count = len(rows)
    if count > 0:
        logger.info("purged %d permanently deleted memories", count)
    return count
