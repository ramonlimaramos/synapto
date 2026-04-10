"""Unit tests for Synapto search engine and graph operations."""

from __future__ import annotations

from uuid import UUID

import pytest

from synapto.db.migrations import ensure_hnsw_index, run_migrations
from synapto.db.postgres import PostgresClient
from synapto.embeddings.sentence_transformer import SentenceTransformerProvider
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

DSN = "postgresql://localhost/synapto"
TENANT = "test_search"


@pytest.fixture
async def pg():
    client = PostgresClient(DSN, min_size=1, max_size=2)
    await client.connect()
    await run_migrations(client)
    yield client
    # cleanup test data
    await client.execute(
        "DELETE FROM memory_entities WHERE memory_id IN (SELECT id FROM memories WHERE tenant = %s);",
        (TENANT,),
    )
    await client.execute("DELETE FROM memories WHERE tenant = %s;", (TENANT,))
    await client.execute(
        "DELETE FROM relations WHERE from_entity_id IN (SELECT id FROM entities WHERE tenant = %s);",
        (TENANT,),
    )
    await client.execute("DELETE FROM entities WHERE tenant = %s;", (TENANT,))
    await client.close()


@pytest.fixture
def provider():
    return SentenceTransformerProvider()


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
