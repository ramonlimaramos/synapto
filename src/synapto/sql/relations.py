"""Statements for ``relations``.

The upserts bind by name; the reads bind positionally. The three ``GET_*``
direction variants share their projection on purpose — the repository picks
one by direction instead of splicing a ``WHERE``. No format slots.
"""

UPSERT = """
    INSERT INTO relations (from_entity_id, to_entity_id, relation_type, weight, metadata)
    VALUES (%(from_id)s, %(to_id)s, %(type)s, %(weight)s, %(meta)s)
    ON CONFLICT (from_entity_id, to_entity_id, relation_type) DO UPDATE SET
        weight = EXCLUDED.weight,
        metadata = relations.metadata || EXCLUDED.metadata
    RETURNING id;
"""

UPSERT_BY_NAME = """
    INSERT INTO relations (from_entity_id, to_entity_id, relation_type, weight)
    SELECT f.id, t.id, %(type)s, %(weight)s
    FROM entities f, entities t
    WHERE f.name = %(from)s AND f.tenant = %(tenant)s
      AND t.name = %(to)s AND t.tenant = %(tenant)s
    ON CONFLICT (from_entity_id, to_entity_id, relation_type) DO UPDATE SET
        weight = EXCLUDED.weight
    RETURNING id;
"""

GET_OUTGOING = """
    SELECT r.id, r.relation_type, r.weight,
           ef.name AS from_entity, et.name AS to_entity
    FROM relations r
    JOIN entities ef ON ef.id = r.from_entity_id
    JOIN entities et ON et.id = r.to_entity_id
    WHERE ef.name = %s AND ef.tenant = %s;
"""

GET_INCOMING = """
    SELECT r.id, r.relation_type, r.weight,
           ef.name AS from_entity, et.name AS to_entity
    FROM relations r
    JOIN entities ef ON ef.id = r.from_entity_id
    JOIN entities et ON et.id = r.to_entity_id
    WHERE et.name = %s AND et.tenant = %s;
"""

GET_BOTH = """
    SELECT r.id, r.relation_type, r.weight,
           ef.name AS from_entity, et.name AS to_entity
    FROM relations r
    JOIN entities ef ON ef.id = r.from_entity_id
    JOIN entities et ON et.id = r.to_entity_id
    WHERE (ef.name = %s OR et.name = %s) AND ef.tenant = %s;
"""

GET_FOR_ENTITIES = """
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

DELETE = "DELETE FROM relations WHERE id = %s RETURNING id;"

COUNT = "SELECT count(*) as cnt FROM relations;"
