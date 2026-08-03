-- migrate:up

ALTER TABLE memories ADD COLUMN IF NOT EXISTS domain VARCHAR(50);

CREATE INDEX IF NOT EXISTS idx_memories_tenant_domain
    ON memories (tenant, domain)
    WHERE deleted_at IS NULL AND domain IS NOT NULL;

-- migrate:down

DROP INDEX IF EXISTS idx_memories_tenant_domain;
ALTER TABLE memories DROP COLUMN IF EXISTS domain;
