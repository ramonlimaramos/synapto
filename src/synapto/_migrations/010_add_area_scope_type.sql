-- migrate:up

-- A seventh scope type: which area of work a memory belongs to.
--
-- The six existing types say where a memory applies — a repository, a
-- language, a product, a skill, a workflow — or that it applies everywhere.
-- None says which discipline it belongs to, and that was invisible only while
-- every memory was software-engineering knowledge. The moment the same agents
-- serve a second area (engineering management, personal finance, writing), a
-- rule such as "every changed file needs a unit test" needs to say "in
-- engineering" without becoming "everywhere", and `global` cannot say that
-- because it does not combine, by design.
--
-- The type list is a CHECK on purpose, mirroring SCOPE_TYPES in Python: a raw
-- write that bypasses the repository must not be able to invent a type no
-- reader filters on. Adding one is therefore a migration, like an origin.
ALTER TABLE memory_scopes
    DROP CONSTRAINT IF EXISTS memory_scopes_type_allowed;
ALTER TABLE memory_scopes
    ADD CONSTRAINT memory_scopes_type_allowed
    CHECK (scope_type IN ('global', 'product', 'repo', 'language', 'skill', 'workflow', 'area'));

-- migrate:down

-- Restoring the six-type CHECK is refused by PostgreSQL while an `area` row
-- exists, and that refusal is the rollback's safety: silently dropping those
-- rows would orphan the applicability the writer asked for. Delete them first,
-- deliberately, then roll back.
ALTER TABLE memory_scopes
    DROP CONSTRAINT IF EXISTS memory_scopes_type_allowed;
ALTER TABLE memory_scopes
    ADD CONSTRAINT memory_scopes_type_allowed
    CHECK (scope_type IN ('global', 'product', 'repo', 'language', 'skill', 'workflow'));
