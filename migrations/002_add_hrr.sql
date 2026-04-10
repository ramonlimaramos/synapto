-- migrate:up

ALTER TABLE memories ADD COLUMN IF NOT EXISTS hrr_vector BYTEA;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS hrr_dim INT;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS trust_score FLOAT NOT NULL DEFAULT 0.5;

CREATE TABLE IF NOT EXISTS memory_banks (
    id SERIAL PRIMARY KEY,
    bank_name VARCHAR(255) NOT NULL UNIQUE,
    vector BYTEA NOT NULL,
    dim INT NOT NULL,
    fact_count INT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memories_trust ON memories (trust_score DESC);
CREATE INDEX IF NOT EXISTS idx_memory_banks_name ON memory_banks (bank_name);

-- migrate:down

DROP INDEX IF EXISTS idx_memory_banks_name;
DROP INDEX IF EXISTS idx_memories_trust;
DROP TABLE IF EXISTS memory_banks CASCADE;
ALTER TABLE memories DROP COLUMN IF EXISTS trust_score;
ALTER TABLE memories DROP COLUMN IF EXISTS hrr_dim;
ALTER TABLE memories DROP COLUMN IF EXISTS hrr_vector;
