-- migrate:up

-- Make metadata queryable. Until now the column was written and read back whole,
-- and the only path that looked inside it was one metadata->>'original_file'
-- read in a migration helper, so "how many memories carry failure_class=X?"
-- could only be answered by fetching a page and counting it client-side — a
-- number capped by the page size, which is a lower bound wearing a count's
-- clothes.
--
-- jsonb_path_ops rather than the default jsonb_ops: it indexes whole paths
-- instead of every individual key and value, which makes it substantially
-- smaller and faster for the containment operator, and containment is the only
-- operator this index exists to serve. The trade is that it cannot answer key-
-- existence queries (?, ?|, ?&) — deliberate, because supporting those would
-- mean supporting a second predicate whose semantics differ from @> in ways a
-- caller would have to reason about.
CREATE INDEX IF NOT EXISTS idx_memories_metadata_gin
    ON memories USING gin (metadata jsonb_path_ops);

-- migrate:down

DROP INDEX IF EXISTS idx_memories_metadata_gin;
