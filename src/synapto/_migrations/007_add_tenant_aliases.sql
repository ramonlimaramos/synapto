-- migrate:up

-- Superseded tenant spellings, so folding the store's twenty-nine tenants into
-- a reviewed list does not make anything already written unreachable.
--
-- This table is NOT a repair path. A non-canonical spelling — wrong case, stray
-- whitespace — is rejected at the boundary by synapto.tenants and never reaches
-- storage. An alias is the other case entirely: a perfectly canonical tenant
-- that a human decided names the same project as another one. 'divergence' is
-- valid and always was; it is here only because someone chose 'podium/divergence'
-- as the survivor.
CREATE TABLE IF NOT EXISTS tenant_aliases (
    -- COLLATE "C" for the same reason memory_scopes carries it: PostgreSQL
    -- documents regex ranges like [a-z] as collating-sequence dependent, so the
    -- CHECKs below would not portably mean "ASCII lowercase" under another
    -- database collation, and a raw write could satisfy SQL and then fail
    -- validation in Python. It also makes key equality byte-for-byte, which is
    -- what a partition key has to be.
    alias VARCHAR(100) COLLATE "C" PRIMARY KEY,
    canonical VARCHAR(100) COLLATE "C" NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- An alias pointing at itself would make resolution a no-op that looks like
    -- a decision, hiding a merge that never happened.
    CONSTRAINT tenant_aliases_no_self_reference CHECK (alias <> canonical),

    -- The row-local half of the contract, mirroring synapto.tenants so a write
    -- that bypasses the module still cannot land. A tenant is either a single
    -- canonical name or 'owner/name'; the owner segment excludes '.' and '_'
    -- while the name segment allows them but must carry at least one
    -- alphanumeric, which also rules out '.' and '..'.
    CONSTRAINT tenant_aliases_alias_grammar CHECK (
        alias ~ '\A[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?\Z'
        OR alias ~ '\A[a-z0-9](?:[a-z0-9-]*[a-z0-9])?/[a-z0-9._-]*[a-z0-9][a-z0-9._-]*\Z'
    ),
    CONSTRAINT tenant_aliases_canonical_grammar CHECK (
        canonical ~ '\A[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?\Z'
        OR canonical ~ '\A[a-z0-9](?:[a-z0-9-]*[a-z0-9])?/[a-z0-9._-]*[a-z0-9][a-z0-9._-]*\Z'
    )
);

-- The invariant this table cannot express row-locally: a canonical must never
-- itself be an alias. A chain would make resolution depend on how many hops a
-- reader is willing to take, and a cycle would make it depend on where the
-- reader started. TenantAliasRepository refuses both under a lock before
-- inserting; see src/synapto/repositories/tenants.py. Resolution stays exactly
-- one hop, so a read costs one lookup and can never loop.

-- Reverse lookups — "what folds into this tenant?" — drive both the merge
-- report and any future unmerge, and are the only access path that is not the
-- primary key.
CREATE INDEX IF NOT EXISTS idx_tenant_aliases_canonical
    ON tenant_aliases (canonical);

-- migrate:down

DROP INDEX IF EXISTS idx_tenant_aliases_canonical;
DROP TABLE IF EXISTS tenant_aliases;
