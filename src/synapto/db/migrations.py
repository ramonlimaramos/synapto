"""Schema migrations for Synapto — creates all tables and indexes."""

from __future__ import annotations

import logging

from synapto.db.postgres import PostgresClient

logger = logging.getLogger("synapto.db.migrations")

SCHEMA_VERSION = 1

MIGRATIONS: list[str] = [
    # --- extensions ---
    "CREATE EXTENSION IF NOT EXISTS vector;",
    "CREATE EXTENSION IF NOT EXISTS pg_trgm;",

    # --- schema version tracking ---
    """
    CREATE TABLE IF NOT EXISTS synapto_schema (
        version INT PRIMARY KEY,
        applied_at TIMESTAMPTZ DEFAULT now()
    );
    """,

    # --- memories ---
    """
    CREATE TABLE IF NOT EXISTS memories (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        content TEXT NOT NULL,
        summary TEXT,
        embedding vector,
        embedding_dim INT,
        type VARCHAR(20) NOT NULL DEFAULT 'general',
        tenant VARCHAR(100) NOT NULL DEFAULT 'default',
        depth_layer VARCHAR(20) NOT NULL DEFAULT 'working',
        metadata JSONB DEFAULT '{}',
        tsv tsvector GENERATED ALWAYS AS (
            to_tsvector('english', coalesce(content, '') || ' ' || coalesce(summary, ''))
        ) STORED,
        decay_score FLOAT NOT NULL DEFAULT 1.0,
        access_count INT NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        accessed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        deleted_at TIMESTAMPTZ
    );
    """,

    # --- entities ---
    """
    CREATE TABLE IF NOT EXISTS entities (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        name VARCHAR(255) NOT NULL,
        entity_type VARCHAR(50) NOT NULL DEFAULT 'concept',
        tenant VARCHAR(100) NOT NULL DEFAULT 'default',
        metadata JSONB DEFAULT '{}',
        embedding vector,
        embedding_dim INT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (name, tenant)
    );
    """,

    # --- relations (graph edges) ---
    """
    CREATE TABLE IF NOT EXISTS relations (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        from_entity_id UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        to_entity_id UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        relation_type VARCHAR(50) NOT NULL DEFAULT 'related_to',
        weight FLOAT NOT NULL DEFAULT 1.0,
        metadata JSONB DEFAULT '{}',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (from_entity_id, to_entity_id, relation_type)
    );
    """,

    # --- memory ↔ entity junction ---
    """
    CREATE TABLE IF NOT EXISTS memory_entities (
        memory_id UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
        entity_id UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        PRIMARY KEY (memory_id, entity_id)
    );
    """,

    # --- indexes: full-text ---
    "CREATE INDEX IF NOT EXISTS idx_memories_tsv ON memories USING gin (tsv);",

    # --- indexes: b-tree ---
    "CREATE INDEX IF NOT EXISTS idx_memories_tenant ON memories (tenant);",
    "CREATE INDEX IF NOT EXISTS idx_memories_depth ON memories (depth_layer);",
    "CREATE INDEX IF NOT EXISTS idx_memories_type ON memories (type);",
    "CREATE INDEX IF NOT EXISTS idx_memories_deleted ON memories (deleted_at);",
    "CREATE INDEX IF NOT EXISTS idx_entities_tenant ON entities (tenant);",
    "CREATE INDEX IF NOT EXISTS idx_entities_type ON entities (entity_type);",

    # --- indexes: graph traversal ---
    "CREATE INDEX IF NOT EXISTS idx_relations_from ON relations (from_entity_id);",
    "CREATE INDEX IF NOT EXISTS idx_relations_to ON relations (to_entity_id);",
    "CREATE INDEX IF NOT EXISTS idx_relations_type ON relations (relation_type);",

    # --- indexes: memory_entities ---
    "CREATE INDEX IF NOT EXISTS idx_memory_entities_entity ON memory_entities (entity_id);",
]

# HNSW indexes are created dynamically based on embedding dimension.
# They cannot use CREATE INDEX IF NOT EXISTS with variable vector size,
# so we handle them separately.
HNSW_INDEX_TEMPLATE = """
    CREATE INDEX IF NOT EXISTS idx_{table}_embedding_{dim}
    ON {table} USING hnsw ((embedding::vector({dim})) vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
"""


async def run_migrations(client: PostgresClient) -> None:
    """Apply all pending migrations."""
    async with client.acquire() as conn:
        for statement in MIGRATIONS:
            await conn.execute(statement)
        await conn.execute(
            "INSERT INTO synapto_schema (version) VALUES (%s) ON CONFLICT (version) DO NOTHING;",
            (SCHEMA_VERSION,),
        )
    logger.info("synapto schema v%d applied", SCHEMA_VERSION)


async def ensure_hnsw_index(client: PostgresClient, dim: int) -> None:
    """Create HNSW indexes for a specific embedding dimension if they don't exist."""
    for table in ("memories", "entities"):
        sql = HNSW_INDEX_TEMPLATE.format(table=table, dim=dim)
        await client.execute(sql)
    logger.info("synapto HNSW indexes ensured for dim=%d", dim)


async def get_schema_version(client: PostgresClient) -> int | None:
    """Return the current schema version, or None if not initialized."""
    try:
        row = await client.execute_one("SELECT max(version) AS v FROM synapto_schema;")
        return row["v"] if row else None
    except Exception:
        return None
