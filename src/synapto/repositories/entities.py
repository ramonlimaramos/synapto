"""Repository for entity CRUD and memory-entity linking.

Design pattern: Repository — isolates all entity-table SQL behind a domain-oriented API.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from synapto.db.postgres import PostgresClient
from synapto.sql import entities as sql


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
            sql.UPSERT,
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
        return await self._db.execute_one(sql.GET_BY_NAME, (name, tenant))

    async def list(
        self,
        tenant: str = "default",
        entity_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        statement = sql.LIST.format(type_filter=sql.LIST_TYPE_FILTER if entity_type else "")
        params = (tenant, entity_type, limit) if entity_type else (tenant, limit)
        return await self._db.execute(statement, params)

    async def delete(self, name: str, tenant: str = "default") -> bool:
        rows = await self._db.execute(sql.DELETE, (name, tenant))
        return len(rows) > 0

    async def link_memory(self, memory_id: UUID, entity_id: UUID) -> None:
        await self._db.execute(sql.LINK_MEMORY, (memory_id, entity_id))

    async def replace_memory_links(self, memory_id: UUID, entity_ids: list[UUID]) -> None:
        await self._db.execute(sql.UNLINK_MEMORY_ENTITIES, (memory_id,))
        if entity_ids:
            await self._db.execute_many(sql.LINK_MEMORY, [(memory_id, entity_id) for entity_id in entity_ids])

    async def get_memory_entities(self, memory_id: UUID) -> list[dict[str, Any]]:
        return await self._db.execute(sql.GET_MEMORY_ENTITIES, (memory_id,))

    async def get_entities_for_memories(self, memory_ids: list[UUID]) -> dict[UUID, list[dict[str, Any]]]:
        if not memory_ids:
            return {}

        rows = await self._db.execute(sql.GET_ENTITIES_FOR_MEMORIES, (memory_ids,))
        grouped: dict[UUID, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["memory_id"], []).append(row)
        return grouped

    async def get_entity_ids_for_memory(self, memory_id: UUID) -> list[UUID]:
        rows = await self._db.execute(sql.GET_ENTITY_IDS_FOR_MEMORY, (memory_id,))
        return [row["entity_id"] for row in rows]

    async def count(self, tenant: str | None = None) -> int:
        if tenant:
            row = await self._db.execute_one(sql.COUNT_IN_TENANT, (tenant,))
        else:
            row = await self._db.execute_one(sql.COUNT)
        return row["cnt"]
