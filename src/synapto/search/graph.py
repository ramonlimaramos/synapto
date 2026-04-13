"""Graph traversal queries using recursive CTEs for N-hop relation walking."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from synapto.db.postgres import PostgresClient

logger = logging.getLogger("synapto.search.graph")


@dataclass
class GraphNode:
    entity_id: UUID
    entity_name: str
    entity_type: str
    depth: int
    path: list[str]
    relation_type: str | None


TRAVERSE_QUERY = """
WITH RECURSIVE graph AS (
    -- base case: start entity
    SELECT
        e.id AS entity_id,
        e.name AS entity_name,
        e.entity_type,
        0 AS depth,
        ARRAY[e.name]::TEXT[] AS path,
        NULL::VARCHAR AS relation_type
    FROM entities e
    WHERE e.name = %(entity_name)s
      AND e.tenant = %(tenant)s

    UNION ALL

    -- recursive: walk outgoing edges
    SELECT
        e2.id,
        e2.name,
        e2.entity_type,
        g.depth + 1,
        g.path || e2.name,
        r.relation_type
    FROM graph g
    JOIN relations r ON r.from_entity_id = g.entity_id
    JOIN entities e2 ON e2.id = r.to_entity_id
    WHERE g.depth < %(max_hops)s
      AND NOT (e2.name = ANY(g.path))
      {relation_filter}
)
SELECT DISTINCT ON (entity_id)
    entity_id, entity_name, entity_type, depth, path, relation_type
FROM graph
ORDER BY entity_id, depth
"""

TRAVERSE_BOTH_DIRECTIONS_QUERY = """
WITH RECURSIVE graph AS (
    SELECT
        e.id AS entity_id,
        e.name AS entity_name,
        e.entity_type,
        0 AS depth,
        ARRAY[e.name]::TEXT[] AS path,
        NULL::VARCHAR AS relation_type
    FROM entities e
    WHERE e.name = %(entity_name)s
      AND e.tenant = %(tenant)s

    UNION ALL

    SELECT
        e2.id, e2.name, e2.entity_type,
        g.depth + 1, g.path || e2.name,
        r.relation_type
    FROM graph g
    JOIN relations r ON (r.from_entity_id = g.entity_id OR r.to_entity_id = g.entity_id)
    JOIN entities e2 ON e2.id = CASE
        WHEN r.from_entity_id = g.entity_id THEN r.to_entity_id
        ELSE r.from_entity_id
    END
    WHERE g.depth < %(max_hops)s AND NOT (e2.name = ANY(g.path))
      {relation_filter}
)
SELECT DISTINCT ON (entity_id)
    entity_id, entity_name, entity_type, depth, path, relation_type
FROM graph
ORDER BY entity_id, depth
"""


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
        relation_filter = "AND r.relation_type = ANY(%(relation_types)s)"
        params["relation_types"] = relation_types

    template = TRAVERSE_BOTH_DIRECTIONS_QUERY if bidirectional else TRAVERSE_QUERY
    sql = template.format(relation_filter=relation_filter)

    rows = await client.execute(sql, params)

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


IMPACT_QUERY = """
WITH RECURSIVE dependents AS (
    SELECT
        e.id, e.name, e.entity_type,
        0 AS depth,
        ARRAY[e.name]::TEXT[] AS path
    FROM entities e
    WHERE e.name = %(entity_name)s AND e.tenant = %(tenant)s

    UNION ALL

    SELECT
        e2.id, e2.name, e2.entity_type,
        d.depth + 1,
        d.path || e2.name
    FROM dependents d
    JOIN relations r ON r.from_entity_id = d.id
        AND r.relation_type IN ('depends_on', 'consumes', 'uses')
    JOIN entities e2 ON e2.id = r.to_entity_id
    WHERE d.depth < %(max_hops)s AND NOT (e2.name = ANY(d.path))
)
SELECT DISTINCT name, entity_type, depth FROM dependents WHERE depth > 0 ORDER BY depth;
"""


async def impact_analysis(
    client: PostgresClient,
    entity_name: str,
    tenant: str = "default",
    max_hops: int = 5,
) -> list[dict[str, Any]]:
    """Find all entities that depend on / are impacted by the given entity."""
    return await client.execute(IMPACT_QUERY, {
        "entity_name": entity_name,
        "tenant": tenant,
        "max_hops": max_hops,
    })
