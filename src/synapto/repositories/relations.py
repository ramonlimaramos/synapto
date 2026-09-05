"""Repository for relation CRUD and graph queries.

Design pattern: Repository — isolates all relation-table SQL behind a domain-oriented API.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from synapto.db.postgres import PostgresClient
from synapto.sql import relations as sql


class RelationRepository:
    """Encapsulates all relation-table SQL operations."""

    def __init__(self, client: PostgresClient) -> None:
        self._db = client

    async def upsert(
        self,
        from_entity_id: UUID,
        to_entity_id: UUID,
        relation_type: str = "related_to",
        weight: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> UUID:
        row = await self._db.execute_one(
            sql.UPSERT,
            {
                "from_id": from_entity_id,
                "to_id": to_entity_id,
                "type": relation_type,
                "weight": weight,
                "meta": Jsonb(metadata or {}),
            },
        )
        return row["id"]

    async def upsert_by_name(
        self,
        from_name: str,
        to_name: str,
        relation_type: str = "related_to",
        tenant: str = "default",
        weight: float = 1.0,
    ) -> UUID | None:
        row = await self._db.execute_one(
            sql.UPSERT_BY_NAME,
            {
                "from": from_name,
                "to": to_name,
                "type": relation_type,
                "tenant": tenant,
                "weight": weight,
            },
        )
        return row["id"] if row else None

    async def get_relations(
        self,
        entity_name: str,
        tenant: str = "default",
        direction: str = "both",
    ) -> list[dict[str, Any]]:
        if direction == "outgoing":
            return await self._db.execute(sql.GET_OUTGOING, (entity_name, tenant))
        elif direction == "incoming":
            return await self._db.execute(sql.GET_INCOMING, (entity_name, tenant))
        return await self._db.execute(sql.GET_BOTH, (entity_name, entity_name, tenant))

    async def get_relations_for_entities(
        self, entity_names: list[str], tenant: str = "default"
    ) -> list[dict[str, Any]]:
        if not entity_names:
            return []

        rows = await self._db.execute(sql.GET_FOR_ENTITIES, (tenant, tenant, entity_names, entity_names))
        seen: set[UUID] = set()
        deduped = []
        for row in rows:
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            deduped.append(row)
        return deduped

    async def delete(self, relation_id: UUID) -> bool:
        rows = await self._db.execute(sql.DELETE, (relation_id,))
        return len(rows) > 0

    async def count(self) -> int:
        row = await self._db.execute_one(sql.COUNT)
        return row["cnt"]
