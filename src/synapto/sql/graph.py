"""Recursive-CTE statements for N-hop relation walking and impact analysis.

Format slots:

* ``TRAVERSE`` and ``TRAVERSE_BOTH_DIRECTIONS`` — ``{relation_filter}``:
  ``RELATION_TYPE_FILTER`` (binds ``relation_types``) or the empty string.

Every walk carries its own ``path`` array and refuses to revisit a name in it,
which is what bounds the recursion on a cyclic graph independently of
``max_hops``. ``IMPACT`` walks only the dependency-flavoured relation types
and has no slots.
"""

TRAVERSE = """
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

TRAVERSE_BOTH_DIRECTIONS = """
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

RELATION_TYPE_FILTER = "AND r.relation_type = ANY(%(relation_types)s)"

IMPACT = """
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
