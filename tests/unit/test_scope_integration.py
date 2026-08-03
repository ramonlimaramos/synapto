"""PR-2: persisting and querying typed scopes through memories and search."""

from __future__ import annotations

import inspect

import pytest

from synapto.db.migrations import ensure_hnsw_index, run_migrations
from synapto.repositories.memories import MemoryRepository
from synapto.repositories.scopes import ScopeRepository, UnknownMemoryError
from synapto.scopes import GLOBAL_KEY, GLOBAL_TYPE, InvalidScopeError, ScopeSet
from synapto.search.hybrid import hybrid_search, vector_search

TENANT = "test_scope_integration"
OTHER_TENANT = "test_scope_integration_other"


@pytest.fixture
async def pg(pg):
    await run_migrations(pg)
    yield pg
    for tenant in (TENANT, OTHER_TENANT):
        await pg.execute("DELETE FROM memories WHERE tenant = %s;", (tenant,))


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
