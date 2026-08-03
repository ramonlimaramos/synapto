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
    -- COLLATE "C" is load-bearing, not decoration. PostgreSQL documents regex
    -- ranges like [a-z] as collating-sequence dependent, so under a non-C
    -- database collation the CHECK below would not portably mean "ASCII
    -- lowercase" — a raw or backfill write could satisfy SQL and then fail
    -- ScopeRef rehydration in Python. Declaring it on the column also makes
    -- primary-key and index equality byte-for-byte, which is what a storage key
    -- has to be.
    scope_type VARCHAR(20) COLLATE "C" NOT NULL,
    scope_key VARCHAR(128) COLLATE "C" NOT NULL,
    source VARCHAR(20) NOT NULL DEFAULT 'explicit',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (memory_id, scope_type, scope_key),

    -- The row-local half of the governed contract, mirroring synapto.scopes so
    -- a write that bypasses the value object still cannot land. Anchored with
    -- \A/\Z rather than ^/$ to state end-of-string explicitly.
    --
    -- Adding a scope type is a migration on purpose: the accepted set is part
    -- of the contract, not configuration.
    CONSTRAINT memory_scopes_type_allowed
        CHECK (scope_type IN ('global', 'product', 'repo', 'language', 'skill', 'workflow')),

    -- Per-type key grammar. 'repo' splits into an owner segment (alphanumeric
    -- with inner hyphens) and a repository segment that may start with a dot —
    -- github/.github is a real repository — but must contain at least one
    -- alphanumeric character, which also rules out '.' and '..'.
    CONSTRAINT memory_scopes_key_grammar CHECK (
        CASE scope_type
            WHEN 'global' THEN scope_key = 'all'
            WHEN 'repo' THEN scope_key ~
                '\A[a-z0-9](?:[a-z0-9-]*[a-z0-9])?/[a-z0-9._-]*[a-z0-9][a-z0-9._-]*\Z'
            ELSE scope_key ~ '\A[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?\Z'
        END
    ),

    CONSTRAINT memory_scopes_source_not_blank
        CHECK (source <> '')
);

-- The aggregate rules — 'global' not combining with other scopes, and the
-- 20-scope cap — are cross-row and cannot be expressed as row-local CHECKs.
-- ScopeRepository enforces them under a FOR UPDATE lock on the parent memory;
-- see src/synapto/repositories/scopes.py.

-- Applicability queries start from the scope and find memories, so the lookup
-- index leads with (scope_type, scope_key); memory_id is included to keep the
-- membership check index-only. The reverse direction (scopes of one memory) is
-- already served by the primary key.
CREATE INDEX IF NOT EXISTS idx_memory_scopes_lookup
    ON memory_scopes (scope_type, scope_key, memory_id);

-- migrate:down

DROP INDEX IF EXISTS idx_memory_scopes_lookup;
DROP TABLE IF EXISTS memory_scopes;
