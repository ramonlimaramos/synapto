"""Statements for ``entities`` and the ``memory_entities`` link table.

Format slots:

* ``LIST`` — ``{type_filter}``: ``LIST_TYPE_FILTER`` or the empty string. When
  the filter is present the caller binds ``(tenant, entity_type, limit)``,
  otherwise ``(tenant, limit)``; the positional order follows the text.

``COUNT`` and ``COUNT_IN_TENANT`` are two statements rather than a base plus a
concatenated ``WHERE``; the caller chooses, it does not append.
"""

UPSERT = """
    INSERT INTO entities (name, entity_type, tenant, metadata, embedding, embedding_dim)
    VALUES (%(name)s, %(type)s, %(tenant)s, %(meta)s, %(emb)s, %(dim)s)
    ON CONFLICT (name, tenant) DO UPDATE SET
        entity_type = EXCLUDED.entity_type,
        metadata = entities.metadata || EXCLUDED.metadata,
        embedding = COALESCE(EXCLUDED.embedding, entities.embedding),
        embedding_dim = COALESCE(EXCLUDED.embedding_dim, entities.embedding_dim)
    RETURNING id;
"""

GET_BY_NAME = "SELECT * FROM entities WHERE name = %s AND tenant = %s;"

LIST = """
    SELECT id, name, entity_type, tenant, metadata, created_at
    FROM entities WHERE tenant = %s {type_filter}
    ORDER BY name LIMIT %s;
"""

LIST_TYPE_FILTER = "AND entity_type = %s"

DELETE = "DELETE FROM entities WHERE name = %s AND tenant = %s RETURNING id;"

LINK_MEMORY = """
    INSERT INTO memory_entities (memory_id, entity_id)
    VALUES (%s, %s) ON CONFLICT DO NOTHING;
"""

UNLINK_MEMORY_ENTITIES = "DELETE FROM memory_entities WHERE memory_id = %s;"

GET_MEMORY_ENTITIES = """
    SELECT e.id, e.name, e.entity_type
    FROM entities e
    JOIN memory_entities me ON me.entity_id = e.id
    WHERE me.memory_id = %s;
"""

GET_ENTITIES_FOR_MEMORIES = """
    SELECT me.memory_id, e.id, e.name, e.entity_type
    FROM memory_entities me
    JOIN entities e ON e.id = me.entity_id
    WHERE me.memory_id = ANY(%s::uuid[])
    ORDER BY me.memory_id, e.name;
"""

COUNT = "SELECT count(*) as cnt FROM entities;"

COUNT_IN_TENANT = "SELECT count(*) as cnt FROM entities WHERE tenant = %s"

GET_ENTITY_IDS_FOR_MEMORY = """
    SELECT entity_id FROM memory_entities WHERE memory_id = %s;
"""
