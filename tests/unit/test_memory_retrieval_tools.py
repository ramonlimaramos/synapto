"""Tests for full-memory retrieval tools used after recall."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError
from psycopg.types.json import Jsonb

from synapto import server
from synapto.db.migrations import run_migrations
from synapto.repositories.entities import EntityRepository
from synapto.repositories.memories import MemoryRepository
from synapto.repositories.relations import RelationRepository

TENANT = "test_memory_retrieval_tools"


class DummyCache:
    def __init__(self) -> None:
        self.invalidated = []

    async def invalidate_memory(self, memory_id):
        self.invalidated.append(memory_id)


async def _cleanup(pg):
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


async def _insert_memory(pg, provider, content: str, *, summary: str | None = None):
    emb = await provider.embed_one(content)
    row = await pg.execute_one(
        """
        INSERT INTO memories (content, summary, embedding, embedding_dim, type, tenant, depth_layer, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;
        """,
        (
            content,
            summary,
            emb,
            provider.dimension,
            "reference",
            TENANT,
            "stable",
            Jsonb({"source": "test"}),
        ),
    )
    return row["id"]


async def test_memory_repository_get_by_id_returns_full_record(pg, provider):
    await run_migrations(pg)
    await _cleanup(pg)
    memory_id = await _insert_memory(pg, provider, "full content survives retrieval", summary="full")

    row = await MemoryRepository(pg).get_by_id(memory_id)

    assert row is not None
    assert row["id"] == memory_id
    assert row["content"] == "full content survives retrieval"
    assert row["summary"] == "full"
    assert row["metadata"] == {"source": "test"}

    await _cleanup(pg)


async def test_memory_repository_update_merges_partial_fields(pg, provider):
    await run_migrations(pg)
    await _cleanup(pg)
    memory_id = await _insert_memory(pg, provider, "original content", summary="old summary")

    row = await MemoryRepository(pg).update(
        memory_id,
        summary="new summary",
        metadata_patch={"status": "updated", "reviewed": True},
    )

    assert row is not None
    assert row["id"] == memory_id
    assert row["content"] == "original content"
    assert row["summary"] == "new summary"
    assert row["metadata"] == {"source": "test", "status": "updated", "reviewed": True}

    await _cleanup(pg)


async def test_update_memory_appends_content_and_invalidates_cache(pg, provider, monkeypatch):
    await run_migrations(pg)
    await _cleanup(pg)
    memory_id = await _insert_memory(pg, provider, "first paragraph", summary="append target")
    cache = DummyCache()
    monkeypatch.setattr(server, "_pg", pg)
    monkeypatch.setattr(server, "_provider", provider)
    monkeypatch.setattr(server, "_cache", cache)

    output = await server.update_memory(str(memory_id), append="\nsecond paragraph")

    row = await MemoryRepository(pg).get_by_id(memory_id)
    assert row is not None
    assert row["content"] == "first paragraph\nsecond paragraph"
    assert f"updated memory {memory_id}" in output
    assert memory_id in cache.invalidated

    await _cleanup(pg)


async def test_update_memory_replaces_entity_links_for_replaced_content(pg, provider, monkeypatch):
    await run_migrations(pg)
    await _cleanup(pg)
    memory_id = await _insert_memory(pg, provider, "`OldEntity` is no longer relevant", summary="entity target")
    ent_repo = EntityRepository(pg)
    old_entity_id = await ent_repo.upsert("OldEntity", tenant=TENANT)
    await ent_repo.link_memory(memory_id, old_entity_id)
    cache = DummyCache()
    monkeypatch.setattr(server, "_pg", pg)
    monkeypatch.setattr(server, "_provider", provider)
    monkeypatch.setattr(server, "_cache", cache)

    await server.update_memory(str(memory_id), content="`NewEntity` is the only relevant entity now")

    entities = await ent_repo.get_memory_entities(memory_id)
    entity_names = {entity["name"] for entity in entities}
    assert "NewEntity" in entity_names
    assert "OldEntity" not in entity_names

    await _cleanup(pg)


async def test_update_memory_updates_summary_and_metadata_patch(pg, provider, monkeypatch):
    await run_migrations(pg)
    await _cleanup(pg)
    memory_id = await _insert_memory(pg, provider, "unchanged content", summary="old summary")
    cache = DummyCache()
    monkeypatch.setattr(server, "_pg", pg)
    monkeypatch.setattr(server, "_cache", cache)

    output = await server.update_memory(
        str(memory_id),
        summary="new summary",
        metadata_patch={"source": "updated", "status": "reviewed"},
    )

    row = await MemoryRepository(pg).get_by_id(memory_id)
    assert row is not None
    assert row["content"] == "unchanged content"
    assert row["summary"] == "new summary"
    assert row["metadata"] == {"source": "updated", "status": "reviewed"}
    assert f"updated memory {memory_id} (summary, metadata)" in output
    assert memory_id in cache.invalidated

    await _cleanup(pg)


async def test_update_memory_rejects_empty_patch():
    with pytest.raises(ToolError, match="provide at least one field to update"):
        await server.update_memory("00000000-0000-0000-0000-000000000000")


async def test_update_memory_rejects_content_and_append_together():
    with pytest.raises(ToolError, match="content and append are mutually exclusive"):
        await server.update_memory(
            "00000000-0000-0000-0000-000000000000",
            content="replacement",
            append="tail",
        )


async def test_remember_rejects_overlong_summary_before_database_access():
    with pytest.raises(ToolError, match="summary exceeds 255 chars \\(got 256\\)"):
        await server.remember("content", summary="x" * 256)


async def test_update_memory_rejects_overlong_summary_before_database_access():
    with pytest.raises(ToolError, match="summary exceeds 255 chars \\(got 256\\)"):
        await server.update_memory("00000000-0000-0000-0000-000000000000", summary="x" * 256)


async def test_get_memory_returns_complete_content_and_entities(pg, provider, monkeypatch):
    await run_migrations(pg)
    await _cleanup(pg)
    content = "Production database access via StrongDM. " + ("details " * 60)
    memory_id = await _insert_memory(pg, provider, content, summary="prod access")

    ent_repo = EntityRepository(pg)
    entity_id = await ent_repo.upsert("StrongDM", tenant=TENANT)
    await ent_repo.link_memory(memory_id, entity_id)
    monkeypatch.setattr(server, "_pg", pg)

    output = await server.get_memory(str(memory_id))

    assert f"id: {memory_id}" in output
    assert "tenant: test_memory_retrieval_tools" in output
    assert "summary: prod access" in output
    assert "entities: StrongDM" in output
    assert "details details details" in output

    await _cleanup(pg)


async def test_get_memory_fetches_relations_in_batch(pg, provider, monkeypatch):
    await run_migrations(pg)
    await _cleanup(pg)
    memory_id = await _insert_memory(pg, provider, "StrongDM exposes Divergence read-only access")

    ent_repo = EntityRepository(pg)
    strongdm_id = await ent_repo.upsert("StrongDM", tenant=TENANT)
    divergence_id = await ent_repo.upsert("Divergence", tenant=TENANT)
    await ent_repo.link_memory(memory_id, strongdm_id)
    await ent_repo.link_memory(memory_id, divergence_id)
    await RelationRepository(pg).upsert_by_name("StrongDM", "Divergence", "provides", TENANT)
    monkeypatch.setattr(server, "_pg", pg)

    output = await server.get_memory(str(memory_id), include_entities=False, include_relations=True)

    assert "relations:" in output
    assert "StrongDM --[provides]--> Divergence" in output
    assert "entities:" not in output

    await _cleanup(pg)


async def test_get_memories_preserves_requested_order_and_reports_missing(pg, provider, monkeypatch):
    await run_migrations(pg)
    await _cleanup(pg)
    first_id = await _insert_memory(pg, provider, "first memory")
    second_id = await _insert_memory(pg, provider, "second memory")
    missing_id = "00000000-0000-0000-0000-000000000000"
    monkeypatch.setattr(server, "_pg", pg)

    output = await server.get_memories([str(second_id), missing_id, str(first_id), "not-a-uuid"])

    second_pos = output.index(f"id: {second_id}")
    missing_pos = output.index(f"memory {missing_id}: not found or deleted")
    first_pos = output.index(f"id: {first_id}")
    invalid_pos = output.index("memory not-a-uuid: invalid id")

    assert second_pos < missing_pos < first_pos < invalid_pos

    await _cleanup(pg)


async def test_get_memories_batches_entities_for_all_rows(pg, provider, monkeypatch):
    await run_migrations(pg)
    await _cleanup(pg)
    first_id = await _insert_memory(pg, provider, "first memory")
    second_id = await _insert_memory(pg, provider, "second memory")

    ent_repo = EntityRepository(pg)
    first_entity_id = await ent_repo.upsert("FirstEntity", tenant=TENANT)
    second_entity_id = await ent_repo.upsert("SecondEntity", tenant=TENANT)
    await ent_repo.link_memory(first_id, first_entity_id)
    await ent_repo.link_memory(second_id, second_entity_id)
    monkeypatch.setattr(server, "_pg", pg)

    output = await server.get_memories([str(first_id), str(second_id)], include_entities=True)

    assert "entities: FirstEntity" in output
    assert "entities: SecondEntity" in output

    await _cleanup(pg)


async def test_get_memories_enforces_bulk_limit():
    ids = ["00000000-0000-0000-0000-000000000000"] * (server.MAX_BULK_MEMORY_IDS + 1)

    with pytest.raises(ToolError, match=f"too many memory ids: max {server.MAX_BULK_MEMORY_IDS}"):
        await server.get_memories(ids)


async def test_recall_preview_zero_uses_explicit_elision_marker(monkeypatch):
    async def fake_hybrid_search(*args, **kwargs):
        return [
            SimpleNamespace(
                id="00000000-0000-0000-0000-000000000001",
                content="hidden content",
                type="reference",
                tenant=TENANT,
                depth_layer="stable",
                decay_score=1.0,
                trust_score=0.5,
                rrf_score=0.1,
                created_at=datetime(2026, 5, 5, tzinfo=UTC),
            )
        ]

    monkeypatch.setattr(server, "_pg", object())
    monkeypatch.setattr(server, "_provider", object())
    monkeypatch.setattr(server, "_config", SimpleNamespace(default_tenant=TENANT))
    monkeypatch.setattr(server, "hybrid_search", fake_hybrid_search)

    output = await server.recall("anything", preview_chars=0)

    assert server.RECALL_CONTENT_ELIDED in output
    assert "hidden content" not in output
