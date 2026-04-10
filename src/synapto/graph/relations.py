"""Relation management for Synapto's knowledge graph."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from synapto.db.postgres import PostgresClient

logger = logging.getLogger("synapto.graph.relations")


async def create_relation(
    client: PostgresClient,
    from_entity_id: UUID,
    to_entity_id: UUID,
    relation_type: str = "related_to",
    weight: float = 1.0,
    metadata: dict[str, Any] | None = None,
) -> UUID:
    """Create a directed relation between two entities."""
    row = await client.execute_one(
        """
        INSERT INTO relations (from_entity_id, to_entity_id, relation_type, weight, metadata)
        VALUES (%(from_id)s, %(to_id)s, %(type)s, %(weight)s, %(meta)s)
        ON CONFLICT (from_entity_id, to_entity_id, relation_type) DO UPDATE SET
            weight = EXCLUDED.weight,
            metadata = relations.metadata || EXCLUDED.metadata
        RETURNING id;
        """,
        {
            "from_id": from_entity_id,
            "to_id": to_entity_id,
            "type": relation_type,
            "weight": weight,
            "meta": Jsonb(metadata or {}),
        },
    )
    return row["id"]


async def create_relation_by_name(
    client: PostgresClient,
    from_name: str,
    to_name: str,
    relation_type: str = "related_to",
    tenant: str = "default",
    weight: float = 1.0,
) -> UUID | None:
    """Create a relation using entity names instead of IDs."""
    row = await client.execute_one(
        """
        INSERT INTO relations (from_entity_id, to_entity_id, relation_type, weight)
        SELECT f.id, t.id, %(type)s, %(weight)s
        FROM entities f, entities t
        WHERE f.name = %(from)s AND f.tenant = %(tenant)s
          AND t.name = %(to)s AND t.tenant = %(tenant)s
        ON CONFLICT (from_entity_id, to_entity_id, relation_type) DO UPDATE SET
            weight = EXCLUDED.weight
        RETURNING id;
        """,
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
    client: PostgresClient,
    entity_name: str,
    tenant: str = "default",
    direction: str = "both",
) -> list[dict[str, Any]]:
    """Get all relations for an entity.

    direction: 'outgoing', 'incoming', or 'both'
    """
    if direction == "outgoing":
        return await client.execute(
            """
            SELECT r.id, r.relation_type, r.weight,
                   ef.name AS from_entity, et.name AS to_entity
            FROM relations r
            JOIN entities ef ON ef.id = r.from_entity_id
            JOIN entities et ON et.id = r.to_entity_id
            WHERE ef.name = %s AND ef.tenant = %s;
            """,
            (entity_name, tenant),
        )
    elif direction == "incoming":
        return await client.execute(
            """
            SELECT r.id, r.relation_type, r.weight,
                   ef.name AS from_entity, et.name AS to_entity
            FROM relations r
            JOIN entities ef ON ef.id = r.from_entity_id
            JOIN entities et ON et.id = r.to_entity_id
            WHERE et.name = %s AND et.tenant = %s;
            """,
            (entity_name, tenant),
        )
    else:
        return await client.execute(
            """
            SELECT r.id, r.relation_type, r.weight,
                   ef.name AS from_entity, et.name AS to_entity
            FROM relations r
            JOIN entities ef ON ef.id = r.from_entity_id
            JOIN entities et ON et.id = r.to_entity_id
            WHERE (ef.name = %s OR et.name = %s) AND ef.tenant = %s;
            """,
            (entity_name, entity_name, tenant),
        )


async def delete_relation(client: PostgresClient, relation_id: UUID) -> bool:
    rows = await client.execute(
        "DELETE FROM relations WHERE id = %s RETURNING id;", (relation_id,)
    )
    return len(rows) > 0
