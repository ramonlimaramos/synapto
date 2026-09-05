"""Statements the command line runs directly, outside the repositories.

``SERVER_VERSION`` and ``PGVECTOR_VERSION`` are the ``synapto doctor`` probes;
neither touches a Synapto table, so both work before the first migration.
``EXPORT_MEMORIES`` and ``IMPORT_MEMORY`` back ``export``, ``import`` and the
flat-file ``migrate`` command, which writes rows without going through
``remember`` so that it can preserve the original type and depth layer.
No format slots.
"""

SERVER_VERSION = "SELECT version() AS v;"

PGVECTOR_VERSION = "SELECT extversion FROM pg_extension WHERE extname = 'vector';"

EXPORT_MEMORIES = """
    SELECT id, content, summary, type, subtype, domain, tenant, depth_layer, metadata, created_at, accessed_at
    FROM memories WHERE deleted_at IS NULL AND tenant = %s ORDER BY created_at;
"""

IMPORT_MEMORY = """
    INSERT INTO memories
        (content, summary, embedding, embedding_dim, type, subtype, domain, tenant, depth_layer, metadata)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
"""
