"""Tests for full-memory retrieval tools used after recall."""

from __future__ import annotations

from psycopg.types.json import Jsonb

from synapto import server
from synapto.db.migrations import run_migrations
from synapto.repositories.entities import EntityRepository
from synapto.repositories.memories import MemoryRepository

TENANT = "test_memory_retrieval_tools"


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


async def test_get_memories_enforces_bulk_limit():
    ids = ["00000000-0000-0000-0000-000000000000"] * (server.MAX_BULK_MEMORY_IDS + 1)

    output = await server.get_memories(ids)

    assert f"too many memory ids: max {server.MAX_BULK_MEMORY_IDS}" in output
