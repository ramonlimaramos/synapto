"""Repository for memory CRUD, search, decay, and trust operations.

Design pattern: Repository — isolates all memory-table SQL behind a domain-oriented API.
Consumers never see raw SQL; they call methods like create(), soft_delete(), update_trust().
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from synapto.db.postgres import PostgresClient

# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------

_INSERT = """
    INSERT INTO memories (content, summary, embedding, embedding_dim, type, tenant, depth_layer, metadata)
    VALUES (%(content)s, %(summary)s, %(emb)s, %(dim)s, %(type)s, %(tenant)s, %(depth)s, %(meta)s)
    RETURNING id;
"""

_GET_BY_ID = """
    SELECT
        id,
        content,
        summary,
        type,
        tenant,
        depth_layer,
        metadata,
        decay_score,
        trust_score,
        access_count,
        created_at,
        accessed_at
    FROM memories
    WHERE id = %s AND deleted_at IS NULL;
"""

_GET_BY_IDS = """
    SELECT
        id,
        content,
        summary,
        type,
        tenant,
        depth_layer,
        metadata,
        decay_score,
        trust_score,
        access_count,
        created_at,
        accessed_at
    FROM memories
    WHERE id = ANY(%s::uuid[]) AND deleted_at IS NULL;
"""

_UPDATE_HRR = "UPDATE memories SET hrr_vector = %s, hrr_dim = %s WHERE id = %s;"

_SOFT_DELETE = """
    UPDATE memories SET deleted_at = now()
    WHERE id = %s AND deleted_at IS NULL
    RETURNING id;
"""

_UPDATE_TRUST = """
    UPDATE memories
    SET trust_score = GREATEST(0.0, LEAST(1.0, trust_score + %s))
    WHERE id = %s AND deleted_at IS NULL
    RETURNING id, trust_score;
"""

_TOUCH_ACCESSED = """
    UPDATE memories SET accessed_at = now(), access_count = access_count + 1
    WHERE id = ANY(%s);
"""

_SELECT_FOR_DECAY = """
    SELECT id, depth_layer, created_at, accessed_at, access_count
    FROM memories
    WHERE deleted_at IS NULL
    ORDER BY accessed_at ASC
    LIMIT %s;
"""

_UPDATE_DECAY_SCORE = "UPDATE memories SET decay_score = %s WHERE id = %s;"

_CLEANUP_EPHEMERAL = """
    UPDATE memories SET deleted_at = now()
    WHERE depth_layer = 'ephemeral'
      AND deleted_at IS NULL
      AND accessed_at < now() - make_interval(hours => %s)
    RETURNING id;
"""

_PURGE_DELETED = """
    DELETE FROM memories
    WHERE deleted_at IS NOT NULL
      AND deleted_at < now() - make_interval(days => %s)
    RETURNING id;
"""

_SELECT_HRR_VECTORS = """
    SELECT hrr_vector FROM memories
    WHERE {where_clause};
"""

_SELECT_WITH_HRR = """
    SELECT id, content, type, tenant, depth_layer, trust_score, hrr_vector
    FROM memories
    WHERE {where_clause}
    LIMIT %s;
"""

_COUNT_BY_TYPE = """
    SELECT type, count(*) as cnt FROM memories
    {where_clause} GROUP BY type ORDER BY cnt DESC;
"""

_COUNT_BY_DEPTH = """
    SELECT depth_layer, count(*) as cnt FROM memories
    {where_clause} GROUP BY depth_layer ORDER BY cnt DESC;
"""

_COUNT_BY_TENANT = """
    SELECT tenant, count(*) as cnt FROM memories
    {where_clause} GROUP BY tenant ORDER BY cnt DESC;
"""

_SELECT_ORIGINAL_FILES = """
    SELECT metadata->>'original_file' AS original_file
    FROM memories
    WHERE tenant = %s
      AND deleted_at IS NULL
      AND metadata ? 'original_file';
"""


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class MemoryRepository:
    """Encapsulates all memory-table SQL operations."""

    def __init__(self, client: PostgresClient) -> None:
        self._db = client

    async def create(
        self,
        content: str,
        embedding: list[float],
        embedding_dim: int,
        memory_type: str,
        tenant: str,
        depth_layer: str,
        summary: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> UUID:
        row = await self._db.execute_one(
            _INSERT,
            {
                "content": content,
                "summary": summary,
                "emb": embedding,
                "dim": embedding_dim,
                "type": memory_type,
                "tenant": tenant,
                "depth": depth_layer,
                "meta": Jsonb(metadata or {}),
            },
        )
        return row["id"]

    async def update_hrr(self, memory_id: UUID, hrr_vector: bytes, hrr_dim: int) -> None:
        await self._db.execute(_UPDATE_HRR, (hrr_vector, hrr_dim, memory_id))

    async def get_by_id(self, memory_id: str | UUID) -> dict[str, Any] | None:
        return await self._db.execute_one(_GET_BY_ID, (memory_id,))

    async def get_by_ids(self, memory_ids: list[str | UUID]) -> list[dict[str, Any]]:
        if not memory_ids:
            return []
        return await self._db.execute(_GET_BY_IDS, (memory_ids,))

    async def soft_delete(self, memory_id: str) -> list[dict]:
        return await self._db.execute(_SOFT_DELETE, (memory_id,))

    async def update_trust(self, memory_id: str, delta: float) -> list[dict]:
        return await self._db.execute(_UPDATE_TRUST, (delta, memory_id))

    async def touch_accessed(self, ids: list[UUID]) -> None:
        await self._db.execute(_TOUCH_ACCESSED, (ids,))

    # -- decay & maintenance --

    async def select_for_decay(self, batch_size: int = 500) -> list[dict]:
        return await self._db.execute(_SELECT_FOR_DECAY, (batch_size,))

    async def update_decay_scores(self, updates: list[tuple[float, UUID]]) -> None:
        await self._db.execute_many(_UPDATE_DECAY_SCORE, updates)

    async def cleanup_ephemeral(self, max_age_hours: int) -> list[dict]:
        return await self._db.execute(_CLEANUP_EPHEMERAL, (max_age_hours,))

    async def purge_deleted(self, older_than_days: int) -> list[dict]:
        return await self._db.execute(_PURGE_DELETED, (older_than_days,))

    # -- hrr vectors --

    async def select_hrr_vectors(
        self, tenant: str, type_filter: str | None = None, depth_filter: str | None = None
    ) -> list[dict]:
        where = ["deleted_at IS NULL", "tenant = %s", "hrr_vector IS NOT NULL"]
        params: list = [tenant]
        if type_filter:
            where.append("type = %s")
            params.append(type_filter)
        if depth_filter:
            where.append("depth_layer = %s")
            params.append(depth_filter)
        sql = _SELECT_HRR_VECTORS.format(where_clause=" AND ".join(where))
        return await self._db.execute(sql, tuple(params))

    # -- hrr retrieval --

    async def select_with_hrr(self, tenant: str, depth_layer: str | None = None, limit: int = 100) -> list[dict]:
        where = ["deleted_at IS NULL", "tenant = %s", "hrr_vector IS NOT NULL"]
        params: list = [tenant]
        if depth_layer:
            where.append("depth_layer = %s")
            params.append(depth_layer)
        sql = _SELECT_WITH_HRR.format(where_clause=" AND ".join(where))
        return await self._db.execute(sql, (*params, limit))

    # -- stats --

    async def count_by_type(self, tenant: str | None = None) -> list[dict]:
        where, params = self._tenant_filter(tenant)
        return await self._db.execute(_COUNT_BY_TYPE.format(where_clause=where), params)

    async def count_by_depth(self, tenant: str | None = None) -> list[dict]:
        where, params = self._tenant_filter(tenant)
        return await self._db.execute(_COUNT_BY_DEPTH.format(where_clause=where), params)

    async def count_by_tenant(self, tenant: str | None = None) -> list[dict]:
        where, params = self._tenant_filter(tenant)
        return await self._db.execute(_COUNT_BY_TENANT.format(where_clause=where), params)

    async def find_existing_original_files(self, tenant: str) -> set[str]:
        rows = await self._db.execute(_SELECT_ORIGINAL_FILES, (tenant,))
        return {r["original_file"] for r in rows if r.get("original_file")}

    @staticmethod
    def _tenant_filter(tenant: str | None) -> tuple[str, tuple]:
        if tenant:
            return "WHERE deleted_at IS NULL AND tenant = %s", (tenant,)
        return "WHERE deleted_at IS NULL", ()
