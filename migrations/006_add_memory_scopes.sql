-- migrate:up

-- Typed N:N applicability for memories. Supersedes the scalar memories.domain
-- added in 005, which is left untouched here: legacy rows keep working and the
-- two coexist until a later migration retires the column.
--
-- Deliberately NOT reusing entities/memory_entities: entity links are heuristic
-- and get replaced when content changes, while a scope is an explicit, durable
-- assertion about where a memory applies.
CREATE TABLE IF NOT EXISTS memory_scopes (
    memory_id UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    scope_type VARCHAR(20) NOT NULL,
    scope_key VARCHAR(128) NOT NULL,
    source VARCHAR(20) NOT NULL DEFAULT 'explicit',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (memory_id, scope_type, scope_key),
    -- defense in depth for the canonical-key rule enforced in synapto.scopes:
    -- a row that bypasses the value object still cannot store uppercase,
    -- whitespace, control, or non-ASCII characters. '/' is permitted here
    -- because repo keys are owner/repo; the per-type rules live in Python.
    CONSTRAINT memory_scopes_key_canonical
        CHECK (scope_key ~ '^[a-z0-9][a-z0-9._/-]*$'),
    CONSTRAINT memory_scopes_type_not_blank
        CHECK (scope_type <> '')
);

-- Applicability queries start from the scope and find memories, so the lookup
-- index leads with (scope_type, scope_key); memory_id is included to keep the
-- membership check index-only. The reverse direction (scopes of one memory) is
-- already served by the primary key.
CREATE INDEX IF NOT EXISTS idx_memory_scopes_lookup
    ON memory_scopes (scope_type, scope_key, memory_id);

-- migrate:down

DROP INDEX IF EXISTS idx_memory_scopes_lookup;
DROP TABLE IF EXISTS memory_scopes;
