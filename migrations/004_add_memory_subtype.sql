-- migrate:up

ALTER TABLE memories ADD COLUMN IF NOT EXISTS subtype VARCHAR(50);

CREATE INDEX IF NOT EXISTS idx_memories_tenant_subtype
    ON memories (tenant, subtype)
    WHERE deleted_at IS NULL AND subtype IS NOT NULL;

-- migrate:down

DROP INDEX IF EXISTS idx_memories_tenant_subtype;
ALTER TABLE memories DROP COLUMN IF EXISTS subtype;
