-- migrate:up

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS synapto_migrations (
    id SERIAL PRIMARY KEY,
    filename VARCHAR(255) NOT NULL UNIQUE,
    checksum VARCHAR(64) NOT NULL,
    applied_at TIMESTAMPTZ DEFAULT now()
);

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

CREATE TABLE IF NOT EXISTS memory_entities (
    memory_id UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    entity_id UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    PRIMARY KEY (memory_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_memories_tsv ON memories USING gin (tsv);
CREATE INDEX IF NOT EXISTS idx_memories_tenant ON memories (tenant);
CREATE INDEX IF NOT EXISTS idx_memories_depth ON memories (depth_layer);
CREATE INDEX IF NOT EXISTS idx_memories_type ON memories (type);
CREATE INDEX IF NOT EXISTS idx_memories_deleted ON memories (deleted_at);
CREATE INDEX IF NOT EXISTS idx_entities_tenant ON entities (tenant);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities (entity_type);
CREATE INDEX IF NOT EXISTS idx_relations_from ON relations (from_entity_id);
CREATE INDEX IF NOT EXISTS idx_relations_to ON relations (to_entity_id);
CREATE INDEX IF NOT EXISTS idx_relations_type ON relations (relation_type);
CREATE INDEX IF NOT EXISTS idx_memory_entities_entity ON memory_entities (entity_id);

-- migrate:down

DROP TABLE IF EXISTS memory_entities CASCADE;
DROP TABLE IF EXISTS relations CASCADE;
DROP TABLE IF EXISTS entities CASCADE;
DROP TABLE IF EXISTS memories CASCADE;
DROP TABLE IF EXISTS synapto_migrations CASCADE;
DROP EXTENSION IF EXISTS pg_trgm;
DROP EXTENSION IF EXISTS vector;
