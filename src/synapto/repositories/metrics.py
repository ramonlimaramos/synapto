"""Repository for metrics_events CRUD.

Design pattern: Repository — isolates all metrics-table SQL behind a domain-oriented API.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from synapto.db.postgres import PostgresClient

# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------

_INSERT = """
    INSERT INTO metrics_events (name, type, value, tags)
    VALUES (%(name)s, %(type)s, %(value)s, %(tags)s);
"""

_LIST_BY_NAME_NO_SINCE = """
    SELECT id, name, type, value, tags, created_at
    FROM metrics_events
    WHERE name = %s
    ORDER BY created_at DESC
    LIMIT %s;
"""

_LIST_BY_NAME_SINCE = """
    SELECT id, name, type, value, tags, created_at
    FROM metrics_events
    WHERE name = %s AND created_at >= %s
    ORDER BY created_at DESC
    LIMIT %s;
"""

_PURGE_OLDER = """
    DELETE FROM metrics_events
    WHERE created_at < now() - make_interval(days => %s)
    RETURNING id;
"""


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class MetricsRepository:
    """Encapsulates all ``metrics_events`` SQL operations."""

    def __init__(self, client: PostgresClient) -> None:
        self._db = client

    async def insert(
        self,
        name: str,
        metric_type: str,
        value: float,
        tags: dict[str, Any],
    ) -> None:
        # ``metric_type`` rather than ``type`` to avoid shadowing the Python builtin.
        # The DB column is still named ``type``; the rename is local to the API.
        await self._db.execute(_INSERT, {
            "name": name,
            "type": metric_type,
            "value": value,
            "tags": Jsonb(tags),
        })

    async def list_by_name(
        self,
        name: str,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if since is None:
            return await self._db.execute(_LIST_BY_NAME_NO_SINCE, (name, limit))
        return await self._db.execute(_LIST_BY_NAME_SINCE, (name, since, limit))

    async def purge_older_than(self, days: int) -> int:
        rows = await self._db.execute(_PURGE_OLDER, (days,))
        return len(rows)
