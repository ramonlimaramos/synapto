-- migrate:up

-- Who wrote this memory. Nothing recorded it before, so a rule the user typed
-- by hand and a rule an automated loop synthesized were indistinguishable once
-- stored. That is tolerable only while every write is human, and stops being
-- tolerable the moment an automated writer exists.
--
-- DEFAULT 'human' is the conservative direction, and the asymmetry is the whole
-- argument: mislabelling an agent write as human costs a memory that survives
-- longer than it should, while the reverse costs a deleted user rule. The
-- backfill below therefore claims every pre-existing row as human — which is
-- also simply true, since no automated writer existed when they were written.
ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS origin VARCHAR(20) COLLATE "C" NOT NULL DEFAULT 'human';

-- A closed vocabulary, enforced in the schema rather than only in Python:
-- provenance is a safety boundary, and a raw write that bypasses the
-- repository must not be able to invent a fourth origin that no pruning rule
-- knows how to treat.
--
-- Adding an origin is a migration on purpose. The set is part of the contract.
ALTER TABLE memories
    DROP CONSTRAINT IF EXISTS memories_origin_allowed;
ALTER TABLE memories
    ADD CONSTRAINT memories_origin_allowed
    CHECK (origin IN ('human', 'agent', 'consolidation'));

-- Reads that filter by origin always also filter by tenant — a loop reads back
-- what it wrote inside one partition — so the index leads with tenant. Partial
-- on live rows because no provenance question is ever asked about soft-deleted
-- ones.
CREATE INDEX IF NOT EXISTS idx_memories_tenant_origin
    ON memories (tenant, origin)
    WHERE deleted_at IS NULL;

-- migrate:down

DROP INDEX IF EXISTS idx_memories_tenant_origin;
ALTER TABLE memories DROP CONSTRAINT IF EXISTS memories_origin_allowed;
ALTER TABLE memories DROP COLUMN IF EXISTS origin;
