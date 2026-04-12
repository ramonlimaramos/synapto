"""Relation management for Synapto's knowledge graph."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from synapto.db.postgres import PostgresClient
from synapto.repositories.relations import RelationRepository

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
    return await RelationRepository(client).upsert(
        from_entity_id=from_entity_id,
        to_entity_id=to_entity_id,
        relation_type=relation_type,
        weight=weight,
        metadata=metadata,
    )


async def create_relation_by_name(
    client: PostgresClient,
    from_name: str,
    to_name: str,
    relation_type: str = "related_to",
    tenant: str = "default",
    weight: float = 1.0,
) -> UUID | None:
    """Create a relation using entity names instead of IDs."""
    return await RelationRepository(client).upsert_by_name(
        from_name=from_name,
        to_name=to_name,
        relation_type=relation_type,
        tenant=tenant,
        weight=weight,
    )


async def get_relations(
    client: PostgresClient,
    entity_name: str,
    tenant: str = "default",
    direction: str = "both",
) -> list[dict[str, Any]]:
    """Get all relations for an entity.

    direction: 'outgoing', 'incoming', or 'both'
    """
    return await RelationRepository(client).get_relations(entity_name, tenant, direction)


async def delete_relation(client: PostgresClient, relation_id: UUID) -> bool:
    return await RelationRepository(client).delete(relation_id)
