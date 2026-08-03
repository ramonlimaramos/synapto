"""Database tests for migration 006 and the scope repository."""

from __future__ import annotations

import pytest
from psycopg import errors as pg_errors

from synapto.db.migrations import run_migrations
from synapto.repositories.scopes import ScopeRepository
from synapto.scopes import GLOBAL_KEY, GLOBAL_TYPE, ScopeRef, ScopeSet

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


class TestMigration006Schema:
    async def test_memory_scopes_table_exists(self, pg):
        row = await pg.execute_one(
            "SELECT to_regclass('public.memory_scopes') AS table_name;"
        )
        assert row["table_name"] == "memory_scopes"

    async def test_lookup_index_leads_with_scope_type_and_key(self, pg):
        row = await pg.execute_one(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_memory_scopes_lookup';"
        )
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
        row = await pg.execute_one(
            "SELECT indexname FROM pg_indexes WHERE indexname = 'idx_memories_tenant_domain';"
        )
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

    async def test_batch_read_avoids_one_query_per_memory(self, pg):
        repo = ScopeRepository(pg)
        first = await _insert_memory(pg, "first")
        second = await _insert_memory(pg, "second")
        unscoped = await _insert_memory(pg, "unscoped")
        await repo.replace(first, _scopes(("language", "python")))
        await repo.replace(second, _scopes(("repo", "owner/repo"), ("skill", "jerry-workday")))

        result = await repo.get_for_memories([first, second, unscoped])

        assert result[first] == _scopes(("language", "python"))
        assert result[second] == _scopes(("repo", "owner/repo"), ("skill", "jerry-workday"))
        # a memory with no scopes is absent rather than mapped to an empty set
        assert unscoped not in result

    async def test_batch_read_of_nothing_touches_no_database(self, pg):
        assert await ScopeRepository(pg).get_for_memories([]) == {}


class TestScopeRepositoryMutations:
    async def test_add_is_idempotent(self, pg):
        memory_id = await _insert_memory(pg)
        repo = ScopeRepository(pg)
        scopes = _scopes(("language", "python"))

        await repo.add(memory_id, scopes)
        await repo.add(memory_id, scopes)

        assert await repo.get_for_memory(memory_id) == scopes

    async def test_add_preserves_existing_memberships(self, pg):
        memory_id = await _insert_memory(pg)
        repo = ScopeRepository(pg)
        await repo.add(memory_id, _scopes(("language", "python")))

        await repo.add(memory_id, _scopes(("repo", "owner/repo")))

        assert await repo.get_for_memory(memory_id) == _scopes(("language", "python"), ("repo", "owner/repo"))

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
        # the delete and the inserts must share one transaction: otherwise a
        # failure part-way through leaves the memory with its old scopes gone
        # and the new ones missing
        memory_id = await _insert_memory(pg)
        repo = ScopeRepository(pg)
        original = _scopes(("language", "python"), ("skill", "jerry-workday"))
        await repo.replace(memory_id, original)

        # bypass the value object to smuggle in a row the CHECK constraint rejects,
        # so the failure happens after the delete and after one successful insert
        poisoned = ScopeSet((ScopeRef("language", "elixir"), ScopeRef("language", "NOT CANONICAL")))

        with pytest.raises(pg_errors.CheckViolation):
            await repo.replace(memory_id, poisoned)

        assert await repo.get_for_memory(memory_id) == original

    async def test_a_failed_replace_does_not_leave_partial_new_scopes(self, pg):
        memory_id = await _insert_memory(pg)
        repo = ScopeRepository(pg)
        await repo.replace(memory_id, _scopes(("language", "python")))

        poisoned = ScopeSet((ScopeRef("repo", "owner/repo"), ScopeRef("language", "Bad Key")))
        with pytest.raises(pg_errors.CheckViolation):
            await repo.replace(memory_id, poisoned)

        stored = await repo.get_for_memory(memory_id)
        assert all(s.scope_key != "owner/repo" for s in stored)
