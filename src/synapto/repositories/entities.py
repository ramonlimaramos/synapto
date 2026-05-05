"""Repository for entity CRUD and memory-entity linking.

Design pattern: Repository — isolates all entity-table SQL behind a domain-oriented API.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from synapto.db.postgres import PostgresClient

# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------

_UPSERT = """
    INSERT INTO entities (name, entity_type, tenant, metadata, embedding, embedding_dim)
    VALUES (%(name)s, %(type)s, %(tenant)s, %(meta)s, %(emb)s, %(dim)s)
    ON CONFLICT (name, tenant) DO UPDATE SET
        entity_type = EXCLUDED.entity_type,
        metadata = entities.metadata || EXCLUDED.metadata,
        embedding = COALESCE(EXCLUDED.embedding, entities.embedding),
        embedding_dim = COALESCE(EXCLUDED.embedding_dim, entities.embedding_dim)
    RETURNING id;
"""

_GET_BY_NAME = "SELECT * FROM entities WHERE name = %s AND tenant = %s;"

_LIST = """
    SELECT id, name, entity_type, tenant, metadata, created_at
    FROM entities WHERE tenant = %s {type_filter}
    ORDER BY name LIMIT %s;
"""

_DELETE = "DELETE FROM entities WHERE name = %s AND tenant = %s RETURNING id;"

_LINK_MEMORY = """
    INSERT INTO memory_entities (memory_id, entity_id)
    VALUES (%s, %s) ON CONFLICT DO NOTHING;
"""

_GET_MEMORY_ENTITIES = """
    SELECT e.id, e.name, e.entity_type
    FROM entities e
    JOIN memory_entities me ON me.entity_id = e.id
    WHERE me.memory_id = %s;
"""

_GET_ENTITIES_FOR_MEMORIES = """
    SELECT me.memory_id, e.id, e.name, e.entity_type
    FROM memory_entities me
    JOIN entities e ON e.id = me.entity_id
    WHERE me.memory_id = ANY(%s::uuid[])
    ORDER BY me.memory_id, e.name;
"""

_COUNT = "SELECT count(*) as cnt FROM entities"

_GET_ENTITY_IDS_FOR_MEMORY = """
    SELECT entity_id FROM memory_entities WHERE memory_id = %s;
"""


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class EntityRepository:
    """Encapsulates all entity-table SQL operations."""

    def __init__(self, client: PostgresClient) -> None:
        self._db = client

    async def upsert(
        self,
        name: str,
        entity_type: str = "concept",
        tenant: str = "default",
        metadata: dict[str, Any] | None = None,
        embedding: list[float] | None = None,
        embedding_dim: int | None = None,
    ) -> UUID:
        row = await self._db.execute_one(
            _UPSERT,
            {
                "name": name,
                "type": entity_type,
                "tenant": tenant,
                "meta": Jsonb(metadata or {}),
                "emb": embedding,
                "dim": embedding_dim,
            },
        )
        return row["id"]

    async def get_by_name(self, name: str, tenant: str = "default") -> dict[str, Any] | None:
        return await self._db.execute_one(_GET_BY_NAME, (name, tenant))

    async def list(
        self,
        tenant: str = "default",
        entity_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        type_filter = "AND entity_type = %s" if entity_type else ""
        sql = _LIST.format(type_filter=type_filter)
        params = (tenant, entity_type, limit) if entity_type else (tenant, limit)
        return await self._db.execute(sql, params)

    async def delete(self, name: str, tenant: str = "default") -> bool:
        rows = await self._db.execute(_DELETE, (name, tenant))
        return len(rows) > 0

    async def link_memory(self, memory_id: UUID, entity_id: UUID) -> None:
        await self._db.execute(_LINK_MEMORY, (memory_id, entity_id))

    async def get_memory_entities(self, memory_id: UUID) -> list[dict[str, Any]]:
        return await self._db.execute(_GET_MEMORY_ENTITIES, (memory_id,))

    async def get_entities_for_memories(self, memory_ids: list[UUID]) -> dict[UUID, list[dict[str, Any]]]:
        if not memory_ids:
            return {}

        rows = await self._db.execute(_GET_ENTITIES_FOR_MEMORIES, (memory_ids,))
        grouped: dict[UUID, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["memory_id"], []).append(row)
        return grouped

    async def get_entity_ids_for_memory(self, memory_id: UUID) -> list[UUID]:
        rows = await self._db.execute(_GET_ENTITY_IDS_FOR_MEMORY, (memory_id,))
        return [row["entity_id"] for row in rows]

    async def count(self, tenant: str | None = None) -> int:
        if tenant:
            row = await self._db.execute_one(_COUNT + " WHERE tenant = %s", (tenant,))
        else:
            row = await self._db.execute_one(_COUNT + ";")
        return row["cnt"]
