"""Decay maintenance — periodic score updates and ephemeral memory cleanup."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from synapto.db.postgres import PostgresClient
from synapto.decay.scoring import calculate_decay_score

logger = logging.getLogger("synapto.decay.maintenance")


async def update_decay_scores(client: PostgresClient, batch_size: int = 500) -> int:
    """Recalculate decay scores for all active memories. Returns count of updated rows."""
    rows = await client.execute(
        """
        SELECT id, depth_layer, created_at, accessed_at, access_count
        FROM memories
        WHERE deleted_at IS NULL
        ORDER BY accessed_at ASC
        LIMIT %s;
        """,
        (batch_size,),
    )

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

    await client.execute_many(
        "UPDATE memories SET decay_score = %s WHERE id = %s;",
        updates,
    )

    logger.info("updated decay scores for %d memories", len(updates))
    return len(updates)


async def cleanup_ephemeral(client: PostgresClient, max_age_hours: int = 24) -> int:
    """Soft-delete ephemeral memories older than max_age_hours."""
    rows = await client.execute(
        """
        UPDATE memories
        SET deleted_at = now()
        WHERE depth_layer = 'ephemeral'
          AND deleted_at IS NULL
          AND accessed_at < now() - make_interval(hours => %s)
        RETURNING id;
        """,
        (max_age_hours,),
    )
    count = len(rows)
    if count > 0:
        logger.info("soft-deleted %d stale ephemeral memories", count)
    return count


async def purge_deleted(client: PostgresClient, older_than_days: int = 30) -> int:
    """Permanently delete soft-deleted memories older than N days."""
    rows = await client.execute(
        """
        DELETE FROM memories
        WHERE deleted_at IS NOT NULL
          AND deleted_at < now() - make_interval(days => %s)
        RETURNING id;
        """,
        (older_than_days,),
    )
    count = len(rows)
    if count > 0:
        logger.info("purged %d permanently deleted memories", count)
    return count
