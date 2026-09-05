"""Graph traversal using recursive CTEs for N-hop relation walking.

The statements live in :mod:`synapto.sql.graph`; this module binds parameters
and chooses the direction and the optional relation-type filter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from synapto.db.postgres import PostgresClient
from synapto.sql import graph as sql

logger = logging.getLogger("synapto.search.graph")


@dataclass
class GraphNode:
    entity_id: UUID
    entity_name: str
    entity_type: str
    depth: int
    path: list[str]
    relation_type: str | None


async def traverse(
    client: PostgresClient,
    entity_name: str,
    tenant: str = "default",
    max_hops: int = 3,
    relation_types: list[str] | None = None,
    bidirectional: bool = True,
) -> list[GraphNode]:
    """Traverse the knowledge graph starting from an entity.

    Args:
        entity_name: starting entity name
        tenant: tenant scope
        max_hops: maximum traversal depth
        relation_types: filter to specific relation types (None = all)
        bidirectional: traverse both incoming and outgoing edges
    """
    relation_filter = ""
    params: dict[str, Any] = {
        "entity_name": entity_name,
        "tenant": tenant,
        "max_hops": max_hops,
    }
    if relation_types:
        relation_filter = sql.RELATION_TYPE_FILTER
        params["relation_types"] = relation_types

    template = sql.TRAVERSE_BOTH_DIRECTIONS if bidirectional else sql.TRAVERSE
    statement = template.format(relation_filter=relation_filter)

    rows = await client.execute(statement, params)

    return [
        GraphNode(
            entity_id=row["entity_id"],
            entity_name=row["entity_name"],
            entity_type=row["entity_type"],
            depth=row["depth"],
            path=row["path"],
            relation_type=row["relation_type"],
        )
        for row in rows
    ]


async def impact_analysis(
    client: PostgresClient,
    entity_name: str,
    tenant: str = "default",
    max_hops: int = 5,
) -> list[dict[str, Any]]:
    """Find all entities that depend on / are impacted by the given entity."""
    return await client.execute(
        sql.IMPACT,
        {
            "entity_name": entity_name,
            "tenant": tenant,
            "max_hops": max_hops,
        },
    )
