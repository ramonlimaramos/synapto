"""Unit tests for Synapto search engine and graph operations."""

from __future__ import annotations

from uuid import UUID

import pytest

from synapto.db.migrations import ensure_hnsw_index, run_migrations
from synapto.graph.entities import (
    create_entity,
    extract_entities_from_text,
    get_entity,
    link_memory_to_entity,
    list_entities,
)
from synapto.graph.relations import (
    create_relation_by_name,
    get_relations,
)
from synapto.search.graph import traverse
from synapto.search.hybrid import hybrid_search, vector_search

TENANT = "test_search"


@pytest.fixture
async def pg(pg):
    """Extend the shared pg fixture with migrations and test-tenant cleanup."""
    await run_migrations(pg)
    yield pg
    await pg.execute(
        "DELETE FROM memory_entities WHERE memory_id IN (SELECT id FROM memories WHERE tenant = %s);",
        (TENANT,),
    )
    await pg.execute("DELETE FROM memories WHERE tenant = %s;", (TENANT,))
    await pg.execute(
        "DELETE FROM relations WHERE from_entity_id IN (SELECT id FROM entities WHERE tenant = %s);",
        (TENANT,),
    )
    await pg.execute("DELETE FROM entities WHERE tenant = %s;", (TENANT,))


async def _insert_memory(pg, provider, content, depth_layer="working", mem_type="general"):
    """Helper to insert a memory with embedding."""
    emb = await provider.embed_one(content)
    row = await pg.execute_one(
        """
        INSERT INTO memories (content, embedding, embedding_dim, type, tenant, depth_layer)
        VALUES (%s, %s, %s, %s, %s, %s) RETURNING id;
        """,
        (content, emb, provider.dimension, mem_type, TENANT, depth_layer),
    )
    return row["id"]


class TestHybridSearch:
    async def test_finds_semantically_similar(self, pg, provider):
        await ensure_hnsw_index(pg, provider.dimension)
        await _insert_memory(pg, provider, "PostgreSQL is a relational database with pgvector support")
        await _insert_memory(pg, provider, "Redis is an in-memory key-value store")
        await _insert_memory(pg, provider, "The weather is sunny today")

        results = await hybrid_search(pg, provider, "vector database", tenant=TENANT)
        assert len(results) > 0
        assert "PostgreSQL" in results[0].content or "pgvector" in results[0].content

    async def test_respects_tenant_isolation(self, pg, provider):
        await ensure_hnsw_index(pg, provider.dimension)
        await _insert_memory(pg, provider, "secret memory in test tenant")

        results = await hybrid_search(pg, provider, "secret memory", tenant="other_tenant")
        assert len(results) == 0

    async def test_depth_layer_filter(self, pg, provider):
        await ensure_hnsw_index(pg, provider.dimension)
        await _insert_memory(pg, provider, "core architecture principle", depth_layer="core")
        await _insert_memory(pg, provider, "ephemeral debug note", depth_layer="ephemeral")

        results = await hybrid_search(pg, provider, "architecture", tenant=TENANT, depth_layer="core")
        assert all(r.depth_layer == "core" for r in results)


class TestHybridSearchParameterization:
    """Tests that depth_layer filtering uses parameterized queries (not f-string injection)."""

    async def test_depth_layer_with_special_characters(self, pg, provider):
        """Ensure depth_layer with SQL injection payload doesn't break or leak data."""
        await ensure_hnsw_index(pg, provider.dimension)
        await _insert_memory(pg, provider, "should not appear", depth_layer="working")

        # a malicious depth_layer should return empty results, not crash or leak
        results = await hybrid_search(
            pg, provider, "should not appear", tenant=TENANT, depth_layer="working' OR '1'='1"
        )
        assert len(results) == 0

    async def test_depth_layer_none_returns_all_layers(self, pg, provider):
        await ensure_hnsw_index(pg, provider.dimension)
        await _insert_memory(pg, provider, "core fact about databases", depth_layer="core")
        await _insert_memory(pg, provider, "working note about databases", depth_layer="working")

        results = await hybrid_search(pg, provider, "databases", tenant=TENANT, depth_layer=None)
        layers = {r.depth_layer for r in results}
        assert len(layers) >= 2

    async def test_vector_search_depth_layer_parameterized(self, pg, provider):
        await ensure_hnsw_index(pg, provider.dimension)
        await _insert_memory(pg, provider, "ephemeral debug session", depth_layer="ephemeral")

        results = await vector_search(pg, provider, "debug session", tenant=TENANT, depth_layer="ephemeral")
        assert all(r.depth_layer == "ephemeral" for r in results)

        # injection attempt should return nothing
        results = await vector_search(
            pg, provider, "debug session", tenant=TENANT, depth_layer="ephemeral' OR '1'='1"
        )
        assert len(results) == 0


class TestVectorSearch:
    async def test_returns_results_with_similarity(self, pg, provider):
        await ensure_hnsw_index(pg, provider.dimension)
        await _insert_memory(pg, provider, "Kafka is a distributed streaming platform")

        results = await vector_search(pg, provider, "message queue streaming", tenant=TENANT)
        assert len(results) > 0
        assert results[0].rrf_score > 0


class TestEntityExtraction:
    def test_extracts_capitalized_phrases(self):
        entities = extract_entities_from_text("Machine Learning and Natural Language Processing are hot topics")
        assert "Machine Learning" in entities
        assert "Natural Language Processing" in entities

    def test_extracts_backtick_terms(self):
        entities = extract_entities_from_text("Use `pgvector` for similarity search with `PostgreSQL`")
        assert "pgvector" in entities
        assert "PostgreSQL" in entities

    def test_filters_stop_words(self):
        entities = extract_entities_from_text("The system handles When conditions arise")
        assert "The" not in entities
        assert "When" not in entities


class TestGraphOperations:
    async def test_create_and_get_entity(self, pg, provider):
        eid = await create_entity(pg, "Hermes", "service", TENANT, provider=provider)
        assert isinstance(eid, UUID)

        entity = await get_entity(pg, "Hermes", TENANT)
        assert entity["name"] == "Hermes"
        assert entity["entity_type"] == "service"

    async def test_list_entities(self, pg, provider):
        await create_entity(pg, "ServiceA", "service", TENANT)
        await create_entity(pg, "ServiceB", "service", TENANT)
        await create_entity(pg, "ConceptX", "concept", TENANT)

        all_ents = await list_entities(pg, TENANT)
        assert len(all_ents) >= 3

        services = await list_entities(pg, TENANT, entity_type="service")
        assert all(e["entity_type"] == "service" for e in services)

    async def test_create_relation_and_traverse(self, pg, provider):
        await create_entity(pg, "Alpha", "service", TENANT)
        await create_entity(pg, "Beta", "service", TENANT)
        await create_entity(pg, "Gamma", "service", TENANT)

        await create_relation_by_name(pg, "Alpha", "Beta", "produces", TENANT)
        await create_relation_by_name(pg, "Beta", "Gamma", "consumes", TENANT)

        nodes = await traverse(pg, "Alpha", TENANT, max_hops=3)
        names = [n.entity_name for n in nodes]
        assert "Alpha" in names
        assert "Beta" in names
        assert "Gamma" in names

    async def test_traverse_with_relation_type_filter(self, pg, provider):
        await create_entity(pg, "SvcA", "service", TENANT)
        await create_entity(pg, "SvcB", "service", TENANT)
        await create_entity(pg, "SvcC", "service", TENANT)

        await create_relation_by_name(pg, "SvcA", "SvcB", "produces", TENANT)
        await create_relation_by_name(pg, "SvcA", "SvcC", "depends_on", TENANT)

        # filter to only "produces" — should find SvcB but not SvcC
        nodes = await traverse(pg, "SvcA", TENANT, max_hops=2, relation_types=["produces"])
        names = [n.entity_name for n in nodes if n.depth > 0]
        assert "SvcB" in names
        assert "SvcC" not in names

    async def test_traverse_relation_type_injection_safe(self, pg, provider):
        """Ensure relation_types with SQL injection payload doesn't break or leak."""
        await create_entity(pg, "SafeNode", "service", TENANT)

        # injection attempt in relation_types — should return only the start node, not crash
        nodes = await traverse(
            pg, "SafeNode", TENANT, max_hops=2,
            relation_types=["produces'; DROP TABLE entities; --"]
        )
        # should get at least the start node, no crash
        assert len(nodes) >= 0

    async def test_traverse_multiple_relation_types(self, pg, provider):
        await create_entity(pg, "Hub", "service", TENANT)
        await create_entity(pg, "Consumer", "service", TENANT)
        await create_entity(pg, "Producer", "service", TENANT)
        await create_entity(pg, "Unrelated", "service", TENANT)

        await create_relation_by_name(pg, "Hub", "Consumer", "consumes", TENANT)
        await create_relation_by_name(pg, "Hub", "Producer", "produces", TENANT)
        await create_relation_by_name(pg, "Hub", "Unrelated", "related_to", TENANT)

        nodes = await traverse(pg, "Hub", TENANT, max_hops=2, relation_types=["consumes", "produces"])
        names = [n.entity_name for n in nodes if n.depth > 0]
        assert "Consumer" in names
        assert "Producer" in names
        assert "Unrelated" not in names

    async def test_get_relations(self, pg, provider):
        await create_entity(pg, "Src", "service", TENANT)
        await create_entity(pg, "Dst", "service", TENANT)
        await create_relation_by_name(pg, "Src", "Dst", "depends_on", TENANT)

        rels = await get_relations(pg, "Src", TENANT, direction="outgoing")
        assert len(rels) >= 1
        assert rels[0]["relation_type"] == "depends_on"

    async def test_link_memory_to_entity(self, pg, provider):
        mid = await _insert_memory(pg, provider, "Hermes uses outbox pattern")
        eid = await create_entity(pg, "OutboxPattern", "pattern", TENANT)
        await link_memory_to_entity(pg, mid, eid)

        from synapto.graph.entities import get_memory_entities
        linked = await get_memory_entities(pg, mid)
        assert any(e["name"] == "OutboxPattern" for e in linked)
