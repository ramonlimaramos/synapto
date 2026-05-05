"""Repository for relation CRUD and graph queries.

Design pattern: Repository — isolates all relation-table SQL behind a domain-oriented API.
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
    INSERT INTO relations (from_entity_id, to_entity_id, relation_type, weight, metadata)
    VALUES (%(from_id)s, %(to_id)s, %(type)s, %(weight)s, %(meta)s)
    ON CONFLICT (from_entity_id, to_entity_id, relation_type) DO UPDATE SET
        weight = EXCLUDED.weight,
        metadata = relations.metadata || EXCLUDED.metadata
    RETURNING id;
"""

_UPSERT_BY_NAME = """
    INSERT INTO relations (from_entity_id, to_entity_id, relation_type, weight)
    SELECT f.id, t.id, %(type)s, %(weight)s
    FROM entities f, entities t
    WHERE f.name = %(from)s AND f.tenant = %(tenant)s
      AND t.name = %(to)s AND t.tenant = %(tenant)s
    ON CONFLICT (from_entity_id, to_entity_id, relation_type) DO UPDATE SET
        weight = EXCLUDED.weight
    RETURNING id;
"""

_GET_OUTGOING = """
    SELECT r.id, r.relation_type, r.weight,
           ef.name AS from_entity, et.name AS to_entity
    FROM relations r
    JOIN entities ef ON ef.id = r.from_entity_id
    JOIN entities et ON et.id = r.to_entity_id
    WHERE ef.name = %s AND ef.tenant = %s;
"""

_GET_INCOMING = """
    SELECT r.id, r.relation_type, r.weight,
           ef.name AS from_entity, et.name AS to_entity
    FROM relations r
    JOIN entities ef ON ef.id = r.from_entity_id
    JOIN entities et ON et.id = r.to_entity_id
    WHERE et.name = %s AND et.tenant = %s;
"""

_GET_BOTH = """
    SELECT r.id, r.relation_type, r.weight,
           ef.name AS from_entity, et.name AS to_entity
    FROM relations r
    JOIN entities ef ON ef.id = r.from_entity_id
    JOIN entities et ON et.id = r.to_entity_id
    WHERE (ef.name = %s OR et.name = %s) AND ef.tenant = %s;
"""

_GET_FOR_ENTITIES = """
    SELECT r.id, r.relation_type, r.weight,
           ef.name AS from_entity, et.name AS to_entity
    FROM relations r
    JOIN entities ef ON ef.id = r.from_entity_id
    JOIN entities et ON et.id = r.to_entity_id
    WHERE ef.tenant = %s
      AND et.tenant = %s
      AND (ef.name = ANY(%s) OR et.name = ANY(%s))
    ORDER BY r.relation_type, ef.name, et.name;
"""

_DELETE = "DELETE FROM relations WHERE id = %s RETURNING id;"

_COUNT = "SELECT count(*) as cnt FROM relations;"


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


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
            _UPSERT,
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
            _UPSERT_BY_NAME,
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
            return await self._db.execute(_GET_OUTGOING, (entity_name, tenant))
        elif direction == "incoming":
            return await self._db.execute(_GET_INCOMING, (entity_name, tenant))
        return await self._db.execute(_GET_BOTH, (entity_name, entity_name, tenant))

    async def get_relations_for_entities(
        self, entity_names: list[str], tenant: str = "default"
    ) -> list[dict[str, Any]]:
        if not entity_names:
            return []

        rows = await self._db.execute(_GET_FOR_ENTITIES, (tenant, tenant, entity_names, entity_names))
        seen: set[UUID] = set()
        deduped = []
        for row in rows:
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            deduped.append(row)
        return deduped

    async def delete(self, relation_id: UUID) -> bool:
        rows = await self._db.execute(_DELETE, (relation_id,))
        return len(rows) > 0

    async def count(self) -> int:
        row = await self._db.execute_one(_COUNT)
        return row["cnt"]
