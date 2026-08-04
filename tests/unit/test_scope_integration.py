"""PR-2: persisting and querying typed scopes through memories and search."""

from __future__ import annotations

import inspect

import pytest
from psycopg import errors as pg_errors

from synapto.db.migrations import ensure_hnsw_index, run_migrations
from synapto.repositories.memories import MemoryRepository
from synapto.repositories.scopes import ScopeRepository, UnknownMemoryError
from synapto.scopes import GLOBAL_KEY, GLOBAL_TYPE, InvalidScopeError, ScopeSet
from synapto.search.hybrid import hybrid_search, vector_search

TENANT = "test_scope_integration"
OTHER_TENANT = "test_scope_integration_other"


@pytest.fixture
async def pg(pg):
    """Leave the table and its HNSW index in a deterministic physical state.

    A per-tenant DELETE leaves dead tuples in the HNSW graph. Because an
    approximate scan applies filters *after* the index scan, that churn made a
    downstream vector test under-return and fail about 9% of the time — the
    tests were the trigger, not the production query.

    TRUNCATE resets the heap and the index instead of tombstoning rows, so the
    state each test starts from is identical. CASCADE covers the tables holding
    a foreign key to memories. This is safe here and only here: the guarded
    fixture has already proven the database is a disposable ``*_test`` one, and
    the suite runs serially with function-scoped data.
    """
    await run_migrations(pg)
    yield pg
    await pg.execute("TRUNCATE TABLE memories CASCADE;")


def _scopes(*pairs) -> ScopeSet:
    return ScopeSet.parse([{"type": t, "key": k} for t, k in pairs])


async def _create(repo, provider, content, *, scopes=None, tenant=TENANT, **kwargs):
    return await repo.create(
        content,
        await provider.embed_one(content),
        provider.dimension,
        "project",
        tenant,
        "working",
        scopes=scopes,
        **kwargs,
    )


class TestAtomicCreation:
    async def test_memory_and_scopes_commit_together(self, pg, provider):
        repo = MemoryRepository(pg)

        memory_id = await _create(repo, provider, "scoped at birth", scopes=_scopes(("language", "python")))

        assert await ScopeRepository(pg).get_for_memory(memory_id) == _scopes(("language", "python"))

    async def test_a_failed_scope_write_rolls_back_the_memory(self, pg, provider, monkeypatch):
        # a memory must never become visible without the scopes that say where
        # it applies, so the parent write has to roll back with them
        repo = MemoryRepository(pg)

        async def boom(*args, **kwargs):
            raise RuntimeError("injected scope failure")

        monkeypatch.setattr(ScopeRepository, "replace_on", boom)

        with pytest.raises(RuntimeError, match="injected"):
            await _create(repo, provider, "must not survive", scopes=_scopes(("language", "python")))

        rows = await pg.execute("SELECT 1 FROM memories WHERE content = %s;", ("must not survive",))
        assert rows == []

    @pytest.mark.parametrize("scopes", [None, ScopeSet()])
    async def test_creation_without_scopes_is_unscoped(self, pg, provider, scopes):
        repo = MemoryRepository(pg)

        memory_id = await _create(repo, provider, "unscoped memory", scopes=scopes)

        assert await ScopeRepository(pg).get_for_memory(memory_id) == ScopeSet()


class TestMutationSemantics:
    async def test_none_preserves_existing_scopes(self, pg, provider):
        repo = MemoryRepository(pg)
        memory_id = await _create(repo, provider, "keeps scopes", scopes=_scopes(("language", "python")))

        await repo.replace_scopes(memory_id, None, tenant=TENANT)

        assert await ScopeRepository(pg).get_for_memory(memory_id) == _scopes(("language", "python"))

    async def test_empty_set_clears_scopes(self, pg, provider):
        repo = MemoryRepository(pg)
        memory_id = await _create(repo, provider, "loses scopes", scopes=_scopes(("language", "python")))

        await repo.replace_scopes(memory_id, ScopeSet(), tenant=TENANT)

        assert await ScopeRepository(pg).get_for_memory(memory_id) == ScopeSet()

    async def test_non_empty_set_replaces_deterministically(self, pg, provider):
        repo = MemoryRepository(pg)
        memory_id = await _create(repo, provider, "rescoped", scopes=_scopes(("language", "python")))

        await repo.replace_scopes(memory_id, _scopes(("repo", "a/b"), ("skill", "jerry-workday")), tenant=TENANT)

        assert await ScopeRepository(pg).get_for_memory(memory_id) == _scopes(
            ("repo", "a/b"), ("skill", "jerry-workday")
        )

    async def test_clear_scopes_removes_everything(self, pg, provider):
        repo = MemoryRepository(pg)
        memory_id = await _create(repo, provider, "cleared", scopes=_scopes(("language", "python")))

        await repo.clear_scopes(memory_id, tenant=TENANT)

        assert await ScopeRepository(pg).get_for_memory(memory_id) == ScopeSet()


class TestTenantAuthorization:
    async def test_another_tenant_cannot_rescope_a_memory(self, pg, provider):
        repo = MemoryRepository(pg)
        memory_id = await _create(repo, provider, "owned elsewhere", scopes=_scopes(("language", "python")))

        with pytest.raises(UnknownMemoryError):
            await repo.replace_scopes(memory_id, _scopes(("repo", "a/b")), tenant=OTHER_TENANT)

        assert await ScopeRepository(pg).get_for_memory(memory_id) == _scopes(("language", "python"))

    async def test_another_tenant_cannot_clear_scopes(self, pg, provider):
        repo = MemoryRepository(pg)
        memory_id = await _create(repo, provider, "owned elsewhere too", scopes=_scopes(("language", "python")))

        with pytest.raises(UnknownMemoryError):
            await repo.clear_scopes(memory_id, tenant=OTHER_TENANT)

        assert await ScopeRepository(pg).get_for_memory(memory_id) == _scopes(("language", "python"))

    async def test_a_soft_deleted_memory_cannot_be_rescoped(self, pg, provider):
        repo = MemoryRepository(pg)
        memory_id = await _create(repo, provider, "deleted", scopes=_scopes(("language", "python")))
        await repo.soft_delete(str(memory_id))

        with pytest.raises(UnknownMemoryError):
            await repo.replace_scopes(memory_id, _scopes(("repo", "a/b")), tenant=TENANT)

    async def test_the_error_does_not_distinguish_wrong_tenant_from_missing(self, pg, provider):
        # confirming a foreign id exists would leak tenancy
        repo = MemoryRepository(pg)
        memory_id = await _create(repo, provider, "real memory")

        with pytest.raises(UnknownMemoryError) as foreign:
            await repo.replace_scopes(memory_id, _scopes(("repo", "a/b")), tenant=OTHER_TENANT)
        with pytest.raises(UnknownMemoryError) as missing:
            await repo.replace_scopes(
                "00000000-0000-0000-0000-000000000000", _scopes(("repo", "a/b")), tenant=OTHER_TENANT
            )

        assert str(foreign.value).replace(str(memory_id), "ID") == str(missing.value).replace(
            "00000000-0000-0000-0000-000000000000", "ID"
        )


class TestRetrievalCarriesScopes:
    async def test_get_by_id_carries_ordered_scopes(self, pg, provider):
        repo = MemoryRepository(pg)
        memory_id = await _create(
            repo, provider, "with scopes", scopes=_scopes(("repo", "a/b"), ("language", "python"))
        )

        row = await repo.get_by_id(memory_id)

        assert [(s.scope_type, s.scope_key) for s in row["scopes"]] == [
            ("language", "python"),
            ("repo", "a/b"),
        ]

    async def test_get_by_id_of_an_unscoped_memory_carries_an_empty_set(self, pg, provider):
        repo = MemoryRepository(pg)
        memory_id = await _create(repo, provider, "no scopes here")

        assert (await repo.get_by_id(memory_id))["scopes"] == ScopeSet()

    async def test_get_by_ids_does_not_query_per_memory(self, pg, provider):
        repo = MemoryRepository(pg)
        ids = [await _create(repo, provider, f"batch {i}", scopes=_scopes(("language", f"lang{i}"))) for i in range(4)]

        counter = _CountingClient(pg)
        rows = await MemoryRepository(counter).get_by_ids(ids)

        # one query for the memories, one for every scope — never one per row
        assert counter.calls == 2
        assert {r["scopes"] for r in rows} == {_scopes(("language", f"lang{i}")) for i in range(4)}

    async def test_get_by_ids_without_scopes_issues_a_single_query(self, pg, provider):
        repo = MemoryRepository(pg)
        memory_id = await _create(repo, provider, "opt out", scopes=_scopes(("language", "python")))

        counter = _CountingClient(pg)
        rows = await MemoryRepository(counter).get_by_ids([memory_id], include_scopes=False)

        assert counter.calls == 1
        assert "scopes" not in rows[0]

    async def test_invalid_stored_aggregate_state_fails_closed(self, pg, provider):
        # global + local is reachable by raw SQL and cannot be a row-local CHECK
        repo = MemoryRepository(pg)
        memory_id = await _create(repo, provider, "corrupt aggregate")
        for scope_type, scope_key in ((GLOBAL_TYPE, GLOBAL_KEY), ("language", "python")):
            await pg.execute(
                "INSERT INTO memory_scopes (memory_id, scope_type, scope_key) VALUES (%s, %s, %s);",
                (memory_id, scope_type, scope_key),
            )

        with pytest.raises(InvalidScopeError):
            await repo.get_by_id(memory_id)


class _CountingClient:
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


class TestApplicabilityFiltering:
    """The Option B truth table, end to end through hybrid search."""

    @pytest.fixture(autouse=True)
    async def corpus(self, pg, provider):
        await ensure_hnsw_index(pg, provider.dimension)
        repo = MemoryRepository(pg)
        self.ids = {}
        fixtures = {
            "unscoped": None,
            "global": _scopes((GLOBAL_TYPE, GLOBAL_KEY)),
            "python": _scopes(("language", "python")),
            "python_or_elixir": _scopes(("language", "python"), ("language", "elixir")),
            "repo_and_python": _scopes(("repo", "a/b"), ("language", "python")),
        }
        for name, scopes in fixtures.items():
            self.ids[await _create(repo, provider, f"tooling note {name}", scopes=scopes)] = name

    async def _search(self, pg, provider, scopes):
        results = await hybrid_search(pg, provider, "tooling note", tenant=TENANT, scopes=scopes, limit=50)
        return sorted(self.ids[r.id] for r in results if r.id in self.ids)

    async def test_same_type_values_are_ored(self, pg, provider):
        assert await self._search(pg, provider, _scopes(("language", "elixir"))) == [
            "global",
            "python_or_elixir",
        ]

    async def test_types_on_the_memory_are_anded(self, pg, provider):
        # repo_and_python requires python too, which this query does not assert
        assert await self._search(pg, provider, _scopes(("repo", "a/b"))) == ["global"]

    async def test_extra_query_types_impose_nothing(self, pg, provider):
        assert await self._search(pg, provider, _scopes(("repo", "a/b"), ("language", "python"))) == [
            "global",
            "python",
            "python_or_elixir",
            "repo_and_python",
        ]

    async def test_global_always_matches(self, pg, provider):
        assert "global" in await self._search(pg, provider, _scopes(("skill", "unrelated")))

    async def test_an_explicit_filter_excludes_unscoped_memories(self, pg, provider):
        assert "unscoped" not in await self._search(pg, provider, _scopes(("language", "python")))

    async def test_no_filter_preserves_legacy_behavior(self, pg, provider):
        results = await hybrid_search(pg, provider, "tooling note", tenant=TENANT, limit=50)

        assert "unscoped" in sorted(self.ids[r.id] for r in results if r.id in self.ids)

    async def test_an_empty_filter_is_invalid(self, pg, provider):
        with pytest.raises(InvalidScopeError, match="empty scope filter"):
            await hybrid_search(pg, provider, "tooling note", tenant=TENANT, scopes=ScopeSet())

    async def test_results_are_not_duplicated_by_the_filter(self, pg, provider):
        # EXISTS/NOT EXISTS rather than a join, so a memory with several
        # matching scopes still appears once and ranking is unperturbed
        results = await hybrid_search(
            pg,
            provider,
            "tooling note",
            tenant=TENANT,
            scopes=_scopes(("language", "python"), ("language", "elixir")),
            limit=50,
        )
        ids = [r.id for r in results]

        assert len(ids) == len(set(ids))

    async def test_vector_search_applies_the_same_rule(self, pg, provider):
        results = await vector_search(
            pg, provider, "tooling note", tenant=TENANT, scopes=_scopes(("repo", "a/b")), limit=50
        )

        assert sorted(self.ids[r.id] for r in results if r.id in self.ids) == ["global"]

    async def test_results_carry_their_scopes(self, pg, provider):
        results = await hybrid_search(
            pg, provider, "tooling note", tenant=TENANT, scopes=_scopes(("language", "python")), limit=50
        )

        by_name = {self.ids[r.id]: r for r in results if r.id in self.ids}
        assert by_name["python"].scopes == _scopes(("language", "python"))
        assert by_name["global"].scopes == _scopes((GLOBAL_TYPE, GLOBAL_KEY))


class TestValidationHappensBeforeSideEffects:
    async def test_an_invalid_filter_costs_no_embedding_and_no_query(self, pg):
        class _Exploding:
            dimension = 384

            async def embed_one(self, text):
                raise AssertionError("embedding must not run for an invalid scope filter")

        counter = _CountingClient(pg)

        with pytest.raises(InvalidScopeError):
            await hybrid_search(counter, _Exploding(), "anything", tenant=TENANT, scopes=ScopeSet())

        assert counter.calls == 0

    def test_scope_parsing_rejects_before_any_io(self):
        # ScopeSet.parse is pure, so a malformed payload cannot reach the DB
        with pytest.raises(InvalidScopeError):
            ScopeSet.parse([{"type": "tenant", "key": "nope"}])


class TestPositionalCompatibility:
    """New optional parameters must not rebind existing positional arguments."""

    def test_search_domain_and_scopes_are_keyword_only(self):
        for func in (hybrid_search, vector_search):
            params = inspect.signature(func).parameters
            assert params["domain"].kind is inspect.Parameter.KEYWORD_ONLY, func.__name__
            assert params["scopes"].kind is inspect.Parameter.KEYWORD_ONLY, func.__name__

    def test_search_positional_order_matches_the_pre_domain_contract(self):
        positional = [
            name
            for name, p in inspect.signature(hybrid_search).parameters.items()
            if p.kind is not inspect.Parameter.KEYWORD_ONLY
        ]

        assert positional == ["client", "provider", "query", "tenant", "depth_layer", "subtype", "limit", "rrf_k"]

    def test_create_domain_and_scopes_are_keyword_only(self):
        params = inspect.signature(MemoryRepository.create).parameters

        assert params["domain"].kind is inspect.Parameter.KEYWORD_ONLY
        assert params["scopes"].kind is inspect.Parameter.KEYWORD_ONLY

    async def test_a_positional_summary_is_still_a_summary(self, pg, provider):
        # before this fix, the seventh positional argument landed in `domain`
        repo = MemoryRepository(pg)
        content = "positional call"

        memory_id = await repo.create(
            content,
            await provider.embed_one(content),
            provider.dimension,
            "project",
            TENANT,
            "working",
            "workflow",
            "a real summary",
        )

        row = await repo.get_by_id(memory_id)
        assert row["summary"] == "a real summary"
        assert row["subtype"] == "workflow"
        assert row["domain"] is None

    async def test_a_positional_limit_is_still_a_limit(self, pg, provider):
        await ensure_hnsw_index(pg, provider.dimension)
        repo = MemoryRepository(pg)
        for index in range(3):
            await _create(repo, provider, f"positional limit probe {index}")

        results = await hybrid_search(pg, provider, "positional limit probe", TENANT, None, None, 2)

        assert len(results) <= 2


class TestScopePayloadRoundTrip:
    """Cache payloads carry ordered scopes and tolerate pre-scope entries."""

    async def test_round_trip_preserves_order(self, cache):
        scopes = _scopes(("repo", "a/b"), ("language", "python"))
        payload = {"content": "cached", "scopes": scopes.to_payload()}

        await cache.cache_memory("11111111-1111-1111-1111-111111111111", payload)
        restored = await cache.get_cached_memory("11111111-1111-1111-1111-111111111111")

        assert ScopeSet.from_payload(restored["scopes"]) == scopes

    async def test_a_legacy_payload_without_scopes_still_reads(self, cache):
        await cache.cache_memory("22222222-2222-2222-2222-222222222222", {"content": "old entry"})

        restored = await cache.get_cached_memory("22222222-2222-2222-2222-222222222222")

        assert ScopeSet.from_payload(restored.get("scopes")) == ScopeSet()

    async def test_a_malformed_scope_payload_is_not_silently_accepted(self, cache):
        await cache.cache_memory(
            "33333333-3333-3333-3333-333333333333", {"scopes": [{"type": "tenant", "key": "nope"}]}
        )

        restored = await cache.get_cached_memory("33333333-3333-3333-3333-333333333333")

        with pytest.raises(InvalidScopeError):
            ScopeSet.from_payload(restored["scopes"])

    async def test_invalidation_drops_a_stale_scope_payload(self, cache):
        memory_id = "44444444-4444-4444-4444-444444444444"
        await cache.cache_memory(memory_id, {"scopes": _scopes(("language", "python")).to_payload()})

        await cache.invalidate_memory(memory_id)

        assert await cache.get_cached_memory(memory_id) is None


class TestCombinedUpdateIsAtomic:
    """Finding 1: memory fields and scopes must commit or roll back together."""

    async def test_fields_and_scopes_commit_together(self, pg, provider):
        repo = MemoryRepository(pg)
        memory_id = await _create(repo, provider, "before", scopes=_scopes(("language", "python")))

        row = await repo.update_with_scopes(memory_id, tenant=TENANT, content="after", scopes=_scopes(("repo", "a/b")))

        assert row["content"] == "after"
        stored = await repo.get_by_id(memory_id)
        assert stored["content"] == "after"
        assert stored["scopes"] == _scopes(("repo", "a/b"))

    async def test_a_scope_failure_rolls_back_the_field_update(self, pg, provider, monkeypatch):
        repo = MemoryRepository(pg)
        memory_id = await _create(repo, provider, "original content", scopes=_scopes(("language", "python")))

        async def boom(*args, **kwargs):
            raise RuntimeError("injected scope failure")

        monkeypatch.setattr(ScopeRepository, "replace_on", boom)

        with pytest.raises(RuntimeError, match="injected"):
            await repo.update_with_scopes(
                memory_id, tenant=TENANT, content="must not persist", scopes=_scopes(("repo", "a/b"))
            )

        stored = await repo.get_by_id(memory_id)
        assert stored["content"] == "original content"
        assert stored["scopes"] == _scopes(("language", "python"))

    async def test_omitted_scopes_preserve_while_fields_change(self, pg, provider):
        repo = MemoryRepository(pg)
        memory_id = await _create(repo, provider, "keep scopes", scopes=_scopes(("language", "python")))

        await repo.update_with_scopes(memory_id, tenant=TENANT, summary="new summary")

        stored = await repo.get_by_id(memory_id)
        assert stored["summary"] == "new summary"
        assert stored["scopes"] == _scopes(("language", "python"))

    async def test_empty_scopes_clear_while_fields_change(self, pg, provider):
        repo = MemoryRepository(pg)
        memory_id = await _create(repo, provider, "clear scopes", scopes=_scopes(("language", "python")))

        await repo.update_with_scopes(memory_id, tenant=TENANT, summary="cleared", scopes=ScopeSet())

        stored = await repo.get_by_id(memory_id)
        assert stored["summary"] == "cleared"
        assert stored["scopes"] == ScopeSet()

    async def test_another_tenant_cannot_update_fields_or_scopes(self, pg, provider):
        repo = MemoryRepository(pg)
        memory_id = await _create(repo, provider, "not yours", scopes=_scopes(("language", "python")))

        with pytest.raises(UnknownMemoryError):
            await repo.update_with_scopes(
                memory_id, tenant=OTHER_TENANT, content="hijacked", scopes=_scopes(("repo", "a/b"))
            )

        stored = await repo.get_by_id(memory_id)
        assert stored["content"] == "not yours"
        assert stored["scopes"] == _scopes(("language", "python"))


class TestAuthorizationLocksTheParent:
    """Finding 2: authorization must not be a read-only check."""

    async def test_authorization_query_takes_a_row_lock(self, pg, provider):
        # a plain SELECT would let a concurrent delete or tenant move land
        # between the check and the scope write
        repo = MemoryRepository(pg)
        memory_id = await _create(repo, provider, "locked parent", scopes=_scopes(("language", "python")))

        async with pg.acquire() as holder:
            await repo._authorize(holder, memory_id, TENANT)

            async with pg.acquire() as contender:
                await contender.execute("SET LOCAL lock_timeout = '250ms';")
                with pytest.raises(pg_errors.LockNotAvailable):
                    await contender.execute("SELECT id FROM memories WHERE id = %s FOR UPDATE;", (memory_id,))
                await contender.rollback()

    async def test_a_delete_committed_before_authorization_wins(self, pg, provider):
        repo = MemoryRepository(pg)
        memory_id = await _create(repo, provider, "deleted first", scopes=_scopes(("language", "python")))
        await repo.soft_delete(str(memory_id))

        with pytest.raises(UnknownMemoryError):
            await repo.replace_scopes(memory_id, _scopes(("repo", "a/b")), tenant=TENANT)

    async def test_an_ownership_change_committed_before_authorization_wins(self, pg, provider):
        repo = MemoryRepository(pg)
        memory_id = await _create(repo, provider, "moved tenant", scopes=_scopes(("language", "python")))
        await pg.execute("UPDATE memories SET tenant = %s WHERE id = %s;", (OTHER_TENANT, memory_id))

        with pytest.raises(UnknownMemoryError):
            await repo.replace_scopes(memory_id, _scopes(("repo", "a/b")), tenant=TENANT)

        assert await ScopeRepository(pg).get_for_memory(memory_id) == _scopes(("language", "python"))

    async def test_a_racing_delete_cannot_land_while_the_parent_is_locked(self, pg, provider):
        repo = MemoryRepository(pg)
        memory_id = await _create(repo, provider, "race target", scopes=_scopes(("language", "python")))

        async with pg.acquire() as holder:
            await repo._authorize(holder, memory_id, TENANT)

            async with pg.acquire() as racer:
                await racer.execute("SET LOCAL lock_timeout = '250ms';")
                with pytest.raises(pg_errors.LockNotAvailable):
                    await racer.execute("UPDATE memories SET deleted_at = now() WHERE id = %s;", (memory_id,))
                await racer.rollback()


class TestDomainAndScopesConflict:
    """Finding 3: the two applicability axes cannot be supplied together."""

    async def test_create_accepts_the_legacy_axis_alone(self, pg, provider):
        repo = MemoryRepository(pg)

        memory_id = await _create(repo, provider, "domain only", domain="python", scopes=None)

        row = await repo.get_by_id(memory_id)
        assert row["domain"] == "python"
        assert row["scopes"] == ScopeSet()

    async def test_create_accepts_the_typed_axis_alone(self, pg, provider):
        repo = MemoryRepository(pg)

        memory_id = await _create(repo, provider, "scopes only", scopes=_scopes(("language", "python")))

        row = await repo.get_by_id(memory_id)
        assert row["domain"] is None
        assert row["scopes"] == _scopes(("language", "python"))

    async def test_create_rejects_both(self, pg, provider):
        repo = MemoryRepository(pg)

        with pytest.raises(InvalidScopeError, match="cannot be combined"):
            await _create(repo, provider, "both axes", domain="python", scopes=_scopes(("language", "python")))

    async def test_an_explicitly_empty_scope_set_still_conflicts(self, pg, provider):
        # empty is a deliberate assertion about scopes, not an absence
        repo = MemoryRepository(pg)

        with pytest.raises(InvalidScopeError, match="cannot be combined"):
            await _create(repo, provider, "empty but supplied", domain="python", scopes=ScopeSet())

    @pytest.mark.parametrize("search", [hybrid_search, vector_search])
    @pytest.mark.parametrize("scopes", [ScopeSet(), None])
    async def test_search_rejects_both_before_any_io(self, pg, search, scopes):
        class _Exploding:
            dimension = 384

            async def embed_one(self, text):
                raise AssertionError("embedding must not run when the arguments conflict")

        counter = _CountingClient(pg)
        supplied = _scopes(("language", "python")) if scopes is None else scopes

        with pytest.raises(InvalidScopeError, match="cannot be combined"):
            await search(counter, _Exploding(), "anything", tenant=TENANT, domain="python", scopes=supplied)

        assert counter.calls == 0

    async def test_stored_legacy_domain_alongside_scopes_still_reads(self, pg, provider):
        # the rejection is about request arguments, not storage: migration 005
        # data coexists until PR-4 backfills it
        repo = MemoryRepository(pg)
        memory_id = await _create(repo, provider, "legacy coexistence", domain="python")
        await repo.replace_scopes(memory_id, _scopes(("language", "python")), tenant=TENANT)

        row = await repo.get_by_id(memory_id)

        assert row["domain"] == "python"
        assert row["scopes"] == _scopes(("language", "python"))


class TestVectorSearchCarriesScopes:
    """Finding 4: vector results dropped their scopes entirely."""

    async def test_results_carry_their_scopes(self, pg, provider):
        await ensure_hnsw_index(pg, provider.dimension)
        repo = MemoryRepository(pg)
        memory_id = await _create(repo, provider, "vector scoped note", scopes=_scopes(("language", "python")))

        results = await vector_search(pg, provider, "vector scoped note", tenant=TENANT, limit=50)

        by_id = {r.id: r for r in results}
        assert by_id[memory_id].scopes == _scopes(("language", "python"))

    async def test_unscoped_results_carry_an_empty_set(self, pg, provider):
        await ensure_hnsw_index(pg, provider.dimension)
        repo = MemoryRepository(pg)
        memory_id = await _create(repo, provider, "vector unscoped note")

        results = await vector_search(pg, provider, "vector unscoped note", tenant=TENANT, limit=50)

        assert {r.id: r for r in results}[memory_id].scopes == ScopeSet()

    async def test_hydration_is_one_query_not_one_per_result(self, pg, provider):
        await ensure_hnsw_index(pg, provider.dimension)
        repo = MemoryRepository(pg)
        for index in range(4):
            await _create(repo, provider, f"vector batch note {index}", scopes=_scopes(("language", f"lang{index}")))

        counter = _CountingClient(pg)
        await vector_search(counter, provider, "vector batch note", tenant=TENANT, limit=50)

        # one search query plus exactly one scope hydration
        assert counter.calls == 2

    async def test_corrupt_aggregate_state_fails_closed(self, pg, provider):
        await ensure_hnsw_index(pg, provider.dimension)
        repo = MemoryRepository(pg)
        memory_id = await _create(repo, provider, "vector corrupt note")
        for scope_type, scope_key in ((GLOBAL_TYPE, GLOBAL_KEY), ("language", "python")):
            await pg.execute(
                "INSERT INTO memory_scopes (memory_id, scope_type, scope_key) VALUES (%s, %s, %s);",
                (memory_id, scope_type, scope_key),
            )

        with pytest.raises(InvalidScopeError):
            await vector_search(pg, provider, "vector corrupt note", tenant=TENANT, limit=50)


class TestGetByIdsContract:
    """Finding 5: typed ids and requested ordering."""

    async def test_accepts_a_mix_of_uuids_and_strings(self, pg, provider):
        repo = MemoryRepository(pg)
        first = await _create(repo, provider, "mixed one")
        second = await _create(repo, provider, "mixed two")

        rows = await repo.get_by_ids([first, str(second)])

        assert [row["id"] for row in rows] == [first, second]

    async def test_returns_rows_in_requested_order(self, pg, provider):
        repo = MemoryRepository(pg)
        ids = [await _create(repo, provider, f"ordered {index}") for index in range(5)]
        requested = list(reversed(ids))

        rows = await repo.get_by_ids(requested)

        assert [row["id"] for row in rows] == requested

    async def test_duplicate_ids_yield_the_row_once(self, pg, provider):
        repo = MemoryRepository(pg)
        memory_id = await _create(repo, provider, "duplicated request")

        rows = await repo.get_by_ids([memory_id, str(memory_id), memory_id])

        assert [row["id"] for row in rows] == [memory_id]

    async def test_missing_ids_are_absent_not_null(self, pg, provider):
        repo = MemoryRepository(pg)
        memory_id = await _create(repo, provider, "present")

        rows = await repo.get_by_ids(["00000000-0000-0000-0000-000000000000", memory_id])

        assert [row["id"] for row in rows] == [memory_id]

    async def test_soft_deleted_ids_are_absent(self, pg, provider):
        repo = MemoryRepository(pg)
        kept = await _create(repo, provider, "kept row")
        removed = await _create(repo, provider, "removed row")
        await repo.soft_delete(str(removed))

        rows = await repo.get_by_ids([removed, kept])

        assert [row["id"] for row in rows] == [kept]

    async def test_ordering_holds_with_scopes_excluded(self, pg, provider):
        repo = MemoryRepository(pg)
        ids = [await _create(repo, provider, f"no scopes {index}") for index in range(3)]
        requested = list(reversed(ids))

        rows = await repo.get_by_ids(requested, include_scopes=False)

        assert [row["id"] for row in rows] == requested
        assert all("scopes" not in row for row in rows)

    async def test_malformed_ids_are_rejected(self, pg):
        with pytest.raises(ValueError):
            await MemoryRepository(pg).get_by_ids(["not-a-uuid"])
