"""Tests for the metadata filter and the uncapped match count.

Two claims are worth proving here, and they are different claims.

The filter must have exactly one reading per value shape. A scalar means
equality and a list means "the stored list contains every element" — both are
what `@>` does, and both are questions a caller actually asks. A nested object
is refused rather than handed to `@>`, because containment on an object matches
a sub-object, which is not the equality the caller asked for.

The count must be *a count*. The failure being replaced is a threshold computed
from a page: it looks like a number, and it silently stops being one as the
store grows past the page size.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from synapto import server
from synapto.search.hybrid import (
    MAX_METADATA_FILTER_KEYS,
    MAX_METADATA_FILTER_LIST_ITEMS,
    InvalidMetadataFilterError,
    count_memories,
    hybrid_search,
    validate_metadata_filter,
)

TENANT = "acme/metadata-filter"


class TestFilterValidation:
    def test_a_flat_mapping_of_scalars_is_accepted(self):
        payload = {"failure_class": "missing_docstring", "count": 3, "resolved": False, "note": None, "score": 1.5}

        assert validate_metadata_filter(payload) == payload

    @pytest.mark.parametrize("bad", ["a string", 7, None, ["failure_class"], True])
    def test_a_non_mapping_is_rejected(self, bad):
        with pytest.raises(InvalidMetadataFilterError, match="must be a JSON object"):
            validate_metadata_filter(bad)

    def test_an_empty_filter_is_rejected_rather_than_matching_everything(self):
        with pytest.raises(InvalidMetadataFilterError, match="matches every memory"):
            validate_metadata_filter({})

    @pytest.mark.parametrize("value", [{"nested": 1}, ("a",), {"a": {"b": {"c": 1}}}])
    def test_a_nested_object_is_rejected(self, value):
        with pytest.raises(InvalidMetadataFilterError, match="only one level of scalar"):
            validate_metadata_filter({"key": value})

    def test_the_nesting_rejection_explains_the_reason(self):
        with pytest.raises(InvalidMetadataFilterError, match="does not mean exact-key equality"):
            validate_metadata_filter({"tags": {"x": 1}})

    def test_the_rejection_names_the_offending_key(self):
        with pytest.raises(InvalidMetadataFilterError, match="'tags'"):
            validate_metadata_filter({"failure_class": "x", "tags": {"y": 1}})

    def test_a_list_of_scalars_is_accepted(self):
        payload = {"products": ["assistant", "inbox"], "counts": [1, 2], "flags": [True, None]}

        assert validate_metadata_filter(payload) == payload

    def test_an_empty_list_is_rejected_rather_than_matching_every_keyed_memory(self):
        with pytest.raises(InvalidMetadataFilterError, match="contained by every array"):
            validate_metadata_filter({"products": []})

    @pytest.mark.parametrize("element", [{"a": 1}, ["nested"], ("t",)])
    def test_a_list_with_a_nested_element_is_rejected(self, element):
        with pytest.raises(InvalidMetadataFilterError, match="list elements must be scalars"):
            validate_metadata_filter({"products": ["ok", element]})

    def test_the_list_length_is_capped(self):
        with pytest.raises(InvalidMetadataFilterError, match=str(MAX_METADATA_FILTER_LIST_ITEMS)):
            validate_metadata_filter({"products": [f"p{i}" for i in range(MAX_METADATA_FILTER_LIST_ITEMS + 1)]})

    def test_the_list_cap_itself_is_accepted(self):
        payload = {"products": [f"p{i}" for i in range(MAX_METADATA_FILTER_LIST_ITEMS)]}

        assert validate_metadata_filter(payload) == payload

    def test_a_non_string_key_is_rejected(self):
        with pytest.raises(InvalidMetadataFilterError, match="keys must be strings"):
            validate_metadata_filter({7: "value"})

    def test_the_key_count_is_capped(self):
        payload = {f"k{i}": i for i in range(MAX_METADATA_FILTER_KEYS + 1)}

        with pytest.raises(InvalidMetadataFilterError, match=str(MAX_METADATA_FILTER_KEYS)):
            validate_metadata_filter(payload)

    def test_the_cap_itself_is_accepted(self):
        payload = {f"k{i}": i for i in range(MAX_METADATA_FILTER_KEYS)}

        assert len(validate_metadata_filter(payload)) == MAX_METADATA_FILTER_KEYS


@pytest.fixture
async def store(pg, provider):
    """Twelve findings of one class and three of another, all in one tenant."""
    await _cleanup(pg)
    embedding = (await provider.embed(["finding"]))[0]
    for index in range(12):
        await _insert(pg, embedding, provider.dimension, f"finding {index}", {"failure_class": "missing_docstring"})
    for index in range(3):
        await _insert(pg, embedding, provider.dimension, f"other {index}", {"failure_class": "long_function"})
    await _insert(pg, embedding, provider.dimension, "unlabelled", {})
    yield pg
    await _cleanup(pg)


async def _cleanup(pg):
    await pg.execute("DELETE FROM memories WHERE tenant = %s;", (TENANT,))


async def _insert(pg, embedding, dim, content, metadata):
    await pg.execute(
        """
        INSERT INTO memories (content, embedding, embedding_dim, type, tenant, depth_layer, metadata)
        VALUES (%s, %s, %s, 'general', %s, 'working', %s);
        """,
        (content, embedding, dim, TENANT, Jsonb(metadata)),
    )


class TestTheCountIsNotCappedByAPage:
    async def test_it_counts_every_match(self, store):
        assert await count_memories(store, tenant=TENANT, metadata_filter={"failure_class": "missing_docstring"}) == 12

    async def test_it_is_unaffected_by_any_page_size(self, store, provider):
        """The regression: a page of 5 must not make the answer 5."""
        page = await hybrid_search(
            store, provider, "finding", tenant=TENANT, limit=5,
            metadata_filter={"failure_class": "missing_docstring"},
        )
        total = await count_memories(store, tenant=TENANT, metadata_filter={"failure_class": "missing_docstring"})

        assert len(page) == 5
        assert total == 12

    async def test_a_different_class_counts_separately(self, store):
        assert await count_memories(store, tenant=TENANT, metadata_filter={"failure_class": "long_function"}) == 3

    async def test_an_absent_class_counts_zero(self, store):
        assert await count_memories(store, tenant=TENANT, metadata_filter={"failure_class": "nonexistent"}) == 0

    async def test_no_filter_counts_the_whole_tenant(self, store):
        assert await count_memories(store, tenant=TENANT) == 16

    async def test_the_count_respects_the_tenant_partition(self, store):
        assert await count_memories(store, tenant="acme/somewhere-else") == 0


class TestContainmentSemantics:
    async def test_a_memory_matches_when_its_metadata_contains_the_pair(self, store, provider):
        results = await hybrid_search(
            store, provider, "finding", tenant=TENANT, limit=50,
            metadata_filter={"failure_class": "missing_docstring"},
        )

        assert len(results) == 12
        assert all(r.metadata["failure_class"] == "missing_docstring" for r in results)

    async def test_extra_keys_on_the_memory_do_not_prevent_a_match(self, store, provider):
        """Containment, not equality: the memory may carry more than was asked."""
        embedding = (await provider.embed(["finding"]))[0]
        await _insert(store, embedding, provider.dimension, "rich finding",
                      {"failure_class": "missing_docstring", "pr": 3875, "reviewer": "bot"})

        assert await count_memories(store, tenant=TENANT, metadata_filter={"failure_class": "missing_docstring"}) == 13

    async def test_every_pair_must_match(self, store, provider):
        embedding = (await provider.embed(["finding"]))[0]
        await _insert(store, embedding, provider.dimension, "two keys",
                      {"failure_class": "missing_docstring", "severity": "low"})

        both = await count_memories(
            store, tenant=TENANT, metadata_filter={"failure_class": "missing_docstring", "severity": "low"}
        )

        assert both == 1

    async def test_a_memory_with_empty_metadata_never_matches_a_filter(self, store):
        assert await count_memories(store, tenant=TENANT, metadata_filter={"failure_class": "long_function"}) == 3

    async def test_the_filter_composes_with_the_other_axes(self, store, provider):
        embedding = (await provider.embed(["finding"]))[0]
        await _insert(store, embedding, provider.dimension, "core finding", {"failure_class": "missing_docstring"})
        await store.execute(
            "UPDATE memories SET depth_layer = 'core' WHERE tenant = %s AND content = %s;", (TENANT, "core finding")
        )

        assert await count_memories(
            store, tenant=TENANT, depth_layer="core", metadata_filter={"failure_class": "missing_docstring"}
        ) == 1


class TestListContainment:
    """A list value means the stored list contains every element; a scalar never matches a list, nor a list a scalar."""

    async def _seed(self, store, provider):
        embedding = (await provider.embed(["finding"]))[0]
        await _insert(store, embedding, provider.dimension, "inbox in acme/mailer",
                      {"products": ["assistant", "inbox"], "language": "elixir"})
        await _insert(store, embedding, provider.dimension, "assistant only",
                      {"products": ["assistant"], "language": "python"})
        await _insert(store, embedding, provider.dimension, "scalar product",
                      {"products": "inbox"})

    async def test_a_list_matches_a_stored_superset(self, store, provider):
        await self._seed(store, provider)

        assert await count_memories(store, tenant=TENANT, metadata_filter={"products": ["inbox"]}) == 1
        assert await count_memories(store, tenant=TENANT, metadata_filter={"products": ["assistant"]}) == 2

    async def test_every_listed_element_must_be_present(self, store, provider):
        await self._seed(store, provider)

        both = await count_memories(
            store, tenant=TENANT, metadata_filter={"products": ["assistant", "inbox"]}
        )
        disjoint = await count_memories(store, tenant=TENANT, metadata_filter={"products": ["billing"]})

        assert both == 1
        assert disjoint == 0

    async def test_a_scalar_filter_does_not_match_a_stored_list(self, store, provider):
        """Equality is unchanged: the scalar reading only matches the memory that stored a scalar."""
        await self._seed(store, provider)

        results = await hybrid_search(
            store, provider, "finding", tenant=TENANT, limit=50, metadata_filter={"products": "inbox"}
        )

        assert [r.content for r in results] == ["scalar product"]

    async def test_a_list_filter_does_not_match_a_stored_scalar(self, store, provider):
        """The converse holds too: containment needs a stored array, a scalar with the same text is not one."""
        await self._seed(store, provider)

        results = await hybrid_search(
            store, provider, "finding", tenant=TENANT, limit=50, metadata_filter={"products": ["inbox"]}
        )

        assert [r.content for r in results] == ["inbox in acme/mailer"]

    async def test_a_list_composes_with_a_scalar_facet(self, store, provider):
        await self._seed(store, provider)

        results = await hybrid_search(
            store, provider, "finding", tenant=TENANT, limit=50,
            metadata_filter={"products": ["assistant"], "language": "elixir"},
        )

        assert [r.content for r in results] == ["inbox in acme/mailer"]


class TestTheIndexIsUsed:
    async def test_explain_reports_the_gin_index_for_containment(self, store):
        """A filter that cannot use the index is a sequential scan wearing a filter's name.

        ``enable_seqscan`` is a session setting and ``store.execute`` takes a
        fresh pool connection per call, so the setting and the ``EXPLAIN`` must
        share one connection; ``SET LOCAL`` scopes it to that transaction so
        nothing leaks back into the pool.
        """
        async with store.acquire() as conn, conn.transaction():
            await conn.execute("SET LOCAL enable_seqscan = off;")
            cursor = await conn.execute(
                "EXPLAIN SELECT id FROM memories WHERE metadata @> %s::jsonb;",
                (Jsonb({"failure_class": "missing_docstring"}),),
            )
            rows = await cursor.fetchall()

        plan = " ".join(r["QUERY PLAN"] for r in rows)

        assert "idx_memories_metadata_gin" in plan

    async def test_explain_reports_the_gin_index_for_a_list_value(self, store):
        async with store.acquire() as conn, conn.transaction():
            await conn.execute("SET LOCAL enable_seqscan = off;")
            cursor = await conn.execute(
                "EXPLAIN SELECT id FROM memories WHERE metadata @> %s::jsonb;",
                (Jsonb({"products": ["inbox"]}),),
            )
            rows = await cursor.fetchall()

        plan = " ".join(r["QUERY PLAN"] for r in rows)

        assert "idx_memories_metadata_gin" in plan


@pytest.fixture
async def wired(store, provider, cache, monkeypatch):
    monkeypatch.setattr(server, "_pg", store)
    monkeypatch.setattr(server, "_provider", provider)
    monkeypatch.setattr(server, "_cache", cache)
    monkeypatch.setattr(server, "_config", SimpleNamespace(default_tenant=TENANT))
    return store


class TestRecallExposesBoth:
    async def test_the_filter_narrows_the_results(self, wired):
        found = await server.recall("finding", tenant=TENANT, metadata_filter={"failure_class": "long_function"})

        assert "other 0" in found
        assert "finding 0" not in found

    async def test_the_headline_reports_the_true_total_not_the_page(self, wired):
        found = await server.recall(
            "finding", tenant=TENANT, limit=5, metadata_filter={"failure_class": "missing_docstring"}
        )

        assert "Recalled 5 memories of 12 matching the filters" in found

    async def test_a_legacy_domain_on_a_result_does_not_poison_the_count(self, wired, provider):
        """The count after a filtered recall must use the caller's arguments, not the last row's.

        A memory written before typed scopes carries a legacy ``domain``; when
        such a row is the last one rendered, the count query used to inherit
        its domain and refuse the caller's ``scopes`` as a conflicting axis.
        """
        embedding = (await provider.embed(["finding"]))[0]
        rows = await wired.execute(
            """
            INSERT INTO memories (content, embedding, embedding_dim, type, tenant, depth_layer, metadata, domain)
            VALUES (%s, %s, %s, 'general', %s, 'working', '{}'::jsonb, 'legacy-domain') RETURNING id;
            """,
            ("scoped legacy finding", embedding, provider.dimension, TENANT),
        )
        await wired.execute(
            "INSERT INTO memory_scopes (memory_id, scope_type, scope_key) VALUES (%s, 'language', 'python');",
            (rows[0]["id"],),
        )

        found = await server.recall("finding", tenant=TENANT, scopes=["language:python"])

        assert "scoped legacy finding" in found
        assert "Recalled 1 memories of 1 matching the filters" in found

    async def test_an_unfiltered_recall_keeps_the_original_headline(self, wired):
        found = await server.recall("finding", tenant=TENANT, limit=5)

        assert "Recalled 5 memories:" in found

    @pytest.mark.parametrize(
        ("bad", "expected"),
        [
            ({"tags": {"x": 1}}, "only one level of scalar"),
            ({"tags": []}, "contained by every array"),
            ({}, "matches every memory"),
            ("failure_class", "must be a JSON object"),
            ([("a", 1)], "must be a JSON object"),
        ],
    )
    async def test_a_malformed_filter_is_a_tool_error(self, wired, bad, expected):
        with pytest.raises(ToolError, match=expected):
            await server.recall("finding", tenant=TENANT, metadata_filter=bad)

    async def test_a_malformed_filter_costs_no_search(self, wired, monkeypatch):
        called = []

        async def fake_search(*_a, **_k):
            called.append(True)
            return []

        monkeypatch.setattr(server, "hybrid_search", fake_search)

        with pytest.raises(ToolError):
            await server.recall("finding", tenant=TENANT, metadata_filter={"tags": {"x": 1}})

        assert called == []

    async def test_a_list_facet_is_reachable_through_the_tool(self, wired, provider):
        embedding = (await provider.embed(["finding"]))[0]
        await _insert(wired, embedding, provider.dimension, "faceted finding",
                      {"products": ["assistant", "inbox"]})

        found = await server.recall("finding", tenant=TENANT, metadata_filter={"products": ["inbox"]})

        assert "faceted finding" in found
        assert "Recalled 1 memories of 1 matching the filters" in found
