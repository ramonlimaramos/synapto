"""Database tests for migration 006 and the scope repository."""

from __future__ import annotations

import pytest
from psycopg import errors as pg_errors

from synapto.db.migrations import run_migrations
from synapto.repositories.scopes import ScopeRepository, UnknownMemoryError
from synapto.scopes import GLOBAL_KEY, GLOBAL_TYPE, InvalidScopeError, ScopeRef, ScopeSet

TENANT = "test_scope_repository"


@pytest.fixture
async def pg(pg):
    """Extend the shared pg fixture with migrations and test-tenant cleanup."""
    await run_migrations(pg)
    yield pg
    await pg.execute("DELETE FROM memories WHERE tenant = %s;", (TENANT,))


async def _insert_memory(pg, content="a memory") -> str:
    row = await pg.execute_one(
        """
        INSERT INTO memories (content, embedding, embedding_dim, type, tenant, depth_layer)
        VALUES (%s, %s, %s, %s, %s, %s) RETURNING id;
        """,
        (content, [0.0] * 384, 384, "project", TENANT, "working"),
    )
    return row["id"]


def _scopes(*pairs) -> ScopeSet:
    return ScopeSet.parse([{"type": t, "key": k} for t, k in pairs])


class _CountingClient:
    """Wraps a real client to prove how many queries an operation issues."""

    def __init__(self, inner):
        self._inner = inner
        self.calls = 0

    async def execute(self, query, params=None):
        self.calls += 1
        return await self._inner.execute(query, params)

    async def execute_one(self, query, params=None):
        self.calls += 1
        return await self._inner.execute_one(query, params)

    def acquire(self):
        return self._inner.acquire()


class _FailingConnection:
    """Wraps a pooled connection and fails deterministically on the Nth execute.

    Fault injection replaces the previous approach of forging invalid value
    objects to provoke a CHECK violation — invalid ScopeRefs can no longer be
    constructed, and depending on them made the test assert against a state the
    domain model forbids.
    """

    def __init__(self, conn, fail_on_call: int):
        self._conn = conn
        self._fail_on_call = fail_on_call
        self.calls = 0

    async def execute(self, query, params=None):
        self.calls += 1
        if self.calls == self._fail_on_call:
            raise RuntimeError("injected mid-transaction failure")
        return await self._conn.execute(query, params)


class TestMigration006Schema:
    async def test_memory_scopes_table_exists(self, pg):
        row = await pg.execute_one("SELECT to_regclass('public.memory_scopes') AS table_name;")
        assert row["table_name"] == "memory_scopes"

    async def test_lookup_index_leads_with_scope_type_and_key(self, pg):
        row = await pg.execute_one("SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_memory_scopes_lookup';")
        assert row is not None
        assert "scope_type, scope_key, memory_id" in row["indexdef"]

    async def test_primary_key_prevents_duplicate_membership(self, pg):
        memory_id = await _insert_memory(pg)
        await pg.execute(
            "INSERT INTO memory_scopes (memory_id, scope_type, scope_key) VALUES (%s, %s, %s);",
            (memory_id, "language", "python"),
        )

        with pytest.raises(pg_errors.UniqueViolation):
            await pg.execute(
                "INSERT INTO memory_scopes (memory_id, scope_type, scope_key) VALUES (%s, %s, %s);",
                (memory_id, "language", "python"),
            )

    async def test_source_defaults_to_explicit(self, pg):
        memory_id = await _insert_memory(pg)
        await pg.execute(
            "INSERT INTO memory_scopes (memory_id, scope_type, scope_key) VALUES (%s, %s, %s);",
            (memory_id, "language", "python"),
        )

        row = await pg.execute_one("SELECT source FROM memory_scopes WHERE memory_id = %s;", (memory_id,))
        assert row["source"] == "explicit"

    @pytest.mark.parametrize("bad_key", ["Python", " python", "py thon", "pythön", "-python"])
    async def test_check_constraint_rejects_non_canonical_keys(self, pg, bad_key):
        # defense in depth: a write that bypasses synapto.scopes still cannot
        # store a key that reads will never match
        memory_id = await _insert_memory(pg)

        with pytest.raises(pg_errors.CheckViolation):
            await pg.execute(
                "INSERT INTO memory_scopes (memory_id, scope_type, scope_key) VALUES (%s, %s, %s);",
                (memory_id, "language", bad_key),
            )

    async def test_foreign_key_cascade_removes_scopes_with_the_memory(self, pg):
        memory_id = await _insert_memory(pg)
        await ScopeRepository(pg).replace(memory_id, _scopes(("language", "python")))

        await pg.execute("DELETE FROM memories WHERE id = %s;", (memory_id,))

        rows = await pg.execute("SELECT 1 FROM memory_scopes WHERE memory_id = %s;", (memory_id,))
        assert rows == []

    async def test_scopes_cannot_reference_a_missing_memory(self, pg):
        with pytest.raises(pg_errors.ForeignKeyViolation):
            await pg.execute(
                "INSERT INTO memory_scopes (memory_id, scope_type, scope_key) VALUES (%s, %s, %s);",
                ("00000000-0000-0000-0000-000000000000", "language", "python"),
            )


class TestMigration005IsPreserved:
    async def test_domain_column_still_exists(self, pg):
        row = await pg.execute_one(
            """
            SELECT data_type, character_maximum_length
            FROM information_schema.columns
            WHERE table_name = 'memories' AND column_name = 'domain';
            """
        )
        assert row is not None
        assert row["character_maximum_length"] == 50

    async def test_domain_index_from_005_still_exists(self, pg):
        row = await pg.execute_one("SELECT indexname FROM pg_indexes WHERE indexname = 'idx_memories_tenant_domain';")
        assert row is not None

    async def test_legacy_domain_values_survive_alongside_scopes(self, pg):
        row = await pg.execute_one(
            """
            INSERT INTO memories (content, embedding, embedding_dim, type, tenant, depth_layer, domain)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id;
            """,
            ("legacy memory", [0.0] * 384, 384, "project", TENANT, "working", "python"),
        )
        memory_id = row["id"]

        await ScopeRepository(pg).replace(memory_id, _scopes(("language", "python")))

        stored = await pg.execute_one("SELECT domain FROM memories WHERE id = %s;", (memory_id,))
        assert stored["domain"] == "python"


class TestScopeRepositoryReads:
    async def test_returns_an_empty_set_for_an_unscoped_memory(self, pg):
        memory_id = await _insert_memory(pg)

        assert await ScopeRepository(pg).get_for_memory(memory_id) == ScopeSet()

    async def test_orders_by_type_then_key(self, pg):
        memory_id = await _insert_memory(pg)
        repo = ScopeRepository(pg)
        await repo.replace(
            memory_id,
            _scopes(("repo", "owner/repo"), ("language", "python"), ("language", "elixir")),
        )

        stored = await repo.get_for_memory(memory_id)

        assert [(s.scope_type, s.scope_key) for s in stored] == [
            ("language", "elixir"),
            ("language", "python"),
            ("repo", "owner/repo"),
        ]

    async def test_batch_read_issues_exactly_one_query(self, pg):
        repo = ScopeRepository(pg)
        first = await _insert_memory(pg, "first")
        second = await _insert_memory(pg, "second")
        unscoped = await _insert_memory(pg, "unscoped")
        await repo.replace(first, _scopes(("language", "python")))
        await repo.replace(second, _scopes(("repo", "owner/repo"), ("skill", "jerry-workday")))

        counter = _CountingClient(pg)
        result = await ScopeRepository(counter).get_for_memories([first, second, unscoped])

        assert counter.calls == 1, "batch read must not issue one query per memory"
        assert result[first] == _scopes(("language", "python"))
        assert result[second] == _scopes(("repo", "owner/repo"), ("skill", "jerry-workday"))
        # a memory with no scopes is absent rather than mapped to an empty set
        assert unscoped not in result

    async def test_batch_read_of_nothing_touches_no_database(self, pg):
        counter = _CountingClient(pg)

        assert await ScopeRepository(counter).get_for_memories([]) == {}
        assert counter.calls == 0


class TestScopeRepositoryMutations:
    async def test_replace_is_idempotent(self, pg):
        memory_id = await _insert_memory(pg)
        repo = ScopeRepository(pg)
        scopes = _scopes(("language", "python"))

        await repo.replace(memory_id, scopes)
        await repo.replace(memory_id, scopes)

        assert await repo.get_for_memory(memory_id) == scopes

    async def test_replace_swaps_the_whole_membership(self, pg):
        memory_id = await _insert_memory(pg)
        repo = ScopeRepository(pg)
        await repo.replace(memory_id, _scopes(("language", "python"), ("skill", "jerry-workday")))

        await repo.replace(memory_id, _scopes(("repo", "owner/repo")))

        assert await repo.get_for_memory(memory_id) == _scopes(("repo", "owner/repo"))

    async def test_replace_with_an_empty_set_clears(self, pg):
        memory_id = await _insert_memory(pg)
        repo = ScopeRepository(pg)
        await repo.replace(memory_id, _scopes(("language", "python")))

        await repo.replace(memory_id, ScopeSet())

        assert await repo.get_for_memory(memory_id) == ScopeSet()

    async def test_clear_removes_everything(self, pg):
        memory_id = await _insert_memory(pg)
        repo = ScopeRepository(pg)
        await repo.replace(memory_id, _scopes(("language", "python"), ("repo", "owner/repo")))

        await repo.clear(memory_id)

        assert await repo.get_for_memory(memory_id) == ScopeSet()

    async def test_mutations_are_scoped_to_one_memory(self, pg):
        repo = ScopeRepository(pg)
        kept = await _insert_memory(pg, "kept")
        cleared = await _insert_memory(pg, "cleared")
        await repo.replace(kept, _scopes(("language", "python")))
        await repo.replace(cleared, _scopes(("language", "elixir")))

        await repo.clear(cleared)

        assert await repo.get_for_memory(kept) == _scopes(("language", "python"))

    async def test_global_scope_round_trips(self, pg):
        memory_id = await _insert_memory(pg)
        repo = ScopeRepository(pg)

        await repo.replace(memory_id, _scopes((GLOBAL_TYPE, GLOBAL_KEY)))

        assert await repo.get_for_memory(memory_id) == ScopeSet((ScopeRef(GLOBAL_TYPE, GLOBAL_KEY),))


class TestReplaceIsAtomic:
    async def test_a_failed_replace_leaves_the_previous_membership_intact(self, pg):
        # the lock, the delete, and the inserts must share one transaction:
        # otherwise a failure part-way leaves the memory with its old scopes
        # gone and the new ones missing
        memory_id = await _insert_memory(pg)
        repo = ScopeRepository(pg)
        original = _scopes(("language", "python"), ("skill", "jerry-workday"))
        await repo.replace(memory_id, original)

        incoming = _scopes(("language", "elixir"), ("repo", "owner/repo"))
        async with pg.acquire() as conn:
            # calls: 1 lock, 2 delete, 3 first insert, 4 second insert
            failing = _FailingConnection(conn, fail_on_call=4)
            with pytest.raises(RuntimeError, match="injected"):
                await repo.replace_on(failing, memory_id, incoming)
            await conn.rollback()

        assert await repo.get_for_memory(memory_id) == original

    async def test_a_failed_replace_does_not_leave_partial_new_scopes(self, pg):
        memory_id = await _insert_memory(pg)
        repo = ScopeRepository(pg)
        await repo.replace(memory_id, _scopes(("language", "python")))

        incoming = _scopes(("language", "elixir"), ("repo", "owner/repo"))
        async with pg.acquire() as conn:
            failing = _FailingConnection(conn, fail_on_call=4)
            with pytest.raises(RuntimeError):
                await repo.replace_on(failing, memory_id, incoming)
            await conn.rollback()

        stored = await repo.get_for_memory(memory_id)
        assert all(s.scope_key not in {"elixir", "owner/repo"} for s in stored)


class TestCallerOwnedTransaction:
    """PR-2 must be able to write a memory and its scopes in one transaction."""

    async def test_memory_and_scopes_commit_together(self, pg):
        repo = ScopeRepository(pg)

        async with pg.acquire() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO memories (content, embedding, embedding_dim, type, tenant, depth_layer)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id;
                """,
                ("written with scopes", [0.0] * 384, 384, "project", TENANT, "working"),
            )
            memory_id = (await cursor.fetchone())["id"]
            await repo.replace_on(conn, memory_id, _scopes(("language", "python")))

        assert await repo.get_for_memory(memory_id) == _scopes(("language", "python"))

    async def test_memory_and_scopes_roll_back_together(self, pg):
        repo = ScopeRepository(pg)
        memory_id = None

        with pytest.raises(RuntimeError, match="caller aborted"):
            async with pg.acquire() as conn:
                cursor = await conn.execute(
                    """
                    INSERT INTO memories (content, embedding, embedding_dim, type, tenant, depth_layer)
                    VALUES (%s, %s, %s, %s, %s, %s) RETURNING id;
                    """,
                    ("should not survive", [0.0] * 384, 384, "project", TENANT, "working"),
                )
                memory_id = (await cursor.fetchone())["id"]
                await repo.replace_on(conn, memory_id, _scopes(("language", "python")))
                raise RuntimeError("caller aborted after both writes")

        memories = await pg.execute("SELECT 1 FROM memories WHERE id = %s;", (memory_id,))
        scopes = await pg.execute("SELECT 1 FROM memory_scopes WHERE memory_id = %s;", (memory_id,))
        assert memories == []
        assert scopes == []


class TestConcurrentReplacesSerialize:
    async def test_two_replaces_do_not_leave_their_union(self, pg):
        # without the FOR UPDATE lock both transactions delete an empty set and
        # then commit different rows, so the memory ends up with both sets
        import asyncio

        memory_id = await _insert_memory(pg)
        repo = ScopeRepository(pg)

        async def replace(scopes, delay):
            async with pg.acquire() as conn:
                await asyncio.sleep(delay)
                await repo.replace_on(conn, memory_id, scopes)

        await asyncio.gather(
            replace(_scopes(("language", "python")), 0),
            replace(_scopes(("repo", "owner/repo")), 0),
        )

        stored = await repo.get_for_memory(memory_id)
        # one replacement wins entirely; the union would be two scopes
        assert len(stored) == 1
        assert stored in (_scopes(("language", "python")), _scopes(("repo", "owner/repo")))


class TestMutationsRequireAnExistingMemory:
    async def test_replace_on_a_missing_memory_is_rejected(self, pg):
        with pytest.raises(UnknownMemoryError):
            await ScopeRepository(pg).replace("00000000-0000-0000-0000-000000000000", _scopes(("language", "python")))

    async def test_clear_on_a_missing_memory_is_rejected(self, pg):
        with pytest.raises(UnknownMemoryError):
            await ScopeRepository(pg).clear("00000000-0000-0000-0000-000000000000")


class TestMemoryIdNormalization:
    async def test_accepts_uuid_objects(self, pg):
        memory_id = await _insert_memory(pg)
        repo = ScopeRepository(pg)
        await repo.replace(memory_id, _scopes(("language", "python")))

        assert await repo.get_for_memories([memory_id]) != {}

    async def test_accepts_string_ids(self, pg):
        memory_id = await _insert_memory(pg)
        repo = ScopeRepository(pg)
        await repo.replace(str(memory_id), _scopes(("language", "python")))

        assert await repo.get_for_memories([str(memory_id)]) != {}

    async def test_accepts_a_mix_of_uuids_and_strings(self, pg):
        # psycopg refuses a heterogeneous list bound to uuid[], and callers
        # naturally produce one when ids come from both the database and JSON
        repo = ScopeRepository(pg)
        first = await _insert_memory(pg, "first")
        second = await _insert_memory(pg, "second")
        await repo.replace(first, _scopes(("language", "python")))
        await repo.replace(second, _scopes(("language", "elixir")))

        result = await repo.get_for_memories([first, str(second)])

        assert set(result) == {first, second}

    async def test_deduplicates_repeated_ids(self, pg):
        memory_id = await _insert_memory(pg)
        repo = ScopeRepository(pg)
        await repo.replace(memory_id, _scopes(("language", "python")))

        result = await repo.get_for_memories([memory_id, str(memory_id), memory_id])

        assert set(result) == {memory_id}

    @pytest.mark.parametrize("bad_id", ["not-a-uuid", ""])
    async def test_rejects_malformed_ids(self, pg, bad_id):
        with pytest.raises(ValueError):
            await ScopeRepository(pg).get_for_memories([bad_id])

    async def test_rejects_non_string_non_uuid_ids(self, pg):
        with pytest.raises(TypeError):
            await ScopeRepository(pg).get_for_memories([7])


class TestReadsFailClosedOnCorruptRows:
    async def test_a_row_violating_the_contract_is_not_returned_as_valid(self, pg):
        # the CHECK constraints make this unreachable through SQL, so the row is
        # planted with the constraint temporarily dropped — the point is that
        # rehydration validates rather than trusting storage
        memory_id = await _insert_memory(pg)
        await pg.execute("ALTER TABLE memory_scopes DROP CONSTRAINT memory_scopes_type_allowed;")
        try:
            await pg.execute(
                "INSERT INTO memory_scopes (memory_id, scope_type, scope_key) VALUES (%s, %s, %s);",
                (memory_id, "tenant", "python"),
            )

            with pytest.raises(InvalidScopeError):
                await ScopeRepository(pg).get_for_memory(memory_id)
        finally:
            await pg.execute("DELETE FROM memory_scopes WHERE memory_id = %s;", (memory_id,))
            await pg.execute(
                """
                ALTER TABLE memory_scopes ADD CONSTRAINT memory_scopes_type_allowed
                CHECK (scope_type IN ('global', 'product', 'repo', 'language', 'skill', 'workflow'));
                """
            )


class TestStorageConstraintsMirrorTheContract:
    @pytest.mark.parametrize("scope_type", ["tenant", "domain", "GLOBAL", "unknown"])
    async def test_unknown_types_are_rejected_by_the_database(self, pg, scope_type):
        memory_id = await _insert_memory(pg)

        with pytest.raises(pg_errors.CheckViolation):
            await pg.execute(
                "INSERT INTO memory_scopes (memory_id, scope_type, scope_key) VALUES (%s, %s, %s);",
                (memory_id, scope_type, "python"),
            )

    @pytest.mark.parametrize("key", ["any", "everything", "python"])
    async def test_global_accepts_only_all_at_the_database_level(self, pg, key):
        memory_id = await _insert_memory(pg)

        with pytest.raises(pg_errors.CheckViolation):
            await pg.execute(
                "INSERT INTO memory_scopes (memory_id, scope_type, scope_key) VALUES (%s, %s, %s);",
                (memory_id, "global", key),
            )

    @pytest.mark.parametrize("key", ["a/b", "owner/repo"])
    async def test_non_repo_types_reject_slashes_at_the_database_level(self, pg, key):
        memory_id = await _insert_memory(pg)

        with pytest.raises(pg_errors.CheckViolation):
            await pg.execute(
                "INSERT INTO memory_scopes (memory_id, scope_type, scope_key) VALUES (%s, %s, %s);",
                (memory_id, "language", key),
            )

    @pytest.mark.parametrize("key", ["synapto", "owner/", "/repo", "owner/repo/extra", "owner/.", "owner/.."])
    async def test_malformed_repo_keys_are_rejected_by_the_database(self, pg, key):
        memory_id = await _insert_memory(pg)

        with pytest.raises(pg_errors.CheckViolation):
            await pg.execute(
                "INSERT INTO memory_scopes (memory_id, scope_type, scope_key) VALUES (%s, %s, %s);",
                (memory_id, "repo", key),
            )

    async def test_canonical_dot_prefixed_repository_is_accepted(self, pg):
        memory_id = await _insert_memory(pg)

        await pg.execute(
            "INSERT INTO memory_scopes (memory_id, scope_type, scope_key) VALUES (%s, %s, %s);",
            (memory_id, "repo", "github/.github"),
        )

        stored = await ScopeRepository(pg).get_for_memory(memory_id)
        assert stored == _scopes(("repo", "github/.github"))

    @pytest.mark.parametrize("key", ["python-", "-python", "python."])
    async def test_trailing_separators_are_rejected_by_the_database(self, pg, key):
        memory_id = await _insert_memory(pg)

        with pytest.raises(pg_errors.CheckViolation):
            await pg.execute(
                "INSERT INTO memory_scopes (memory_id, scope_type, scope_key) VALUES (%s, %s, %s);",
                (memory_id, "language", key),
            )
