"""Tests for write origin and the destructive paths that read it.

Origin is a safety boundary, not bookkeeping, so the assertions are mostly
about refusal and about what is *not* done: never inferring the value after the
fact, and never deleting a human-authored memory without being told to, twice.

`forget` had no test at all before this. That is worth stating, because it is
the one tool whose failure mode is unrecoverable.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError

from synapto import server
from synapto.provenance import (
    AGENT,
    AUTOMATED_ORIGINS,
    CONSOLIDATION,
    DEFAULT_ORIGIN,
    HUMAN,
    ORIGINS,
    InvalidOriginError,
    is_automated,
    validate_origin,
)
from synapto.repositories.memories import MemoryRepository
from synapto.search.hybrid import count_memories

TENANT = "acme/provenance"


class TestTheVocabularyIsClosed:
    @pytest.mark.parametrize("origin", ORIGINS)
    def test_every_declared_origin_is_accepted(self, origin):
        assert validate_origin(origin) == origin

    @pytest.mark.parametrize("origin", ["Human", "HUMAN", "bot", "", " human", "user", None, 7, True])
    def test_anything_else_is_rejected(self, origin):
        with pytest.raises(InvalidOriginError):
            validate_origin(origin)

    def test_the_rejection_lists_the_choices(self):
        with pytest.raises(InvalidOriginError, match="human, agent, consolidation"):
            validate_origin("bot")

    def test_the_default_is_the_conservative_one(self):
        """Mislabelling an agent write costs a stale memory; the reverse costs a rule."""
        assert DEFAULT_ORIGIN == HUMAN

    def test_human_is_not_an_automated_origin(self):
        assert not is_automated(HUMAN)

    @pytest.mark.parametrize("origin", AUTOMATED_ORIGINS)
    def test_automated_origins_are_deletable_by_a_pass(self, origin):
        assert is_automated(origin)

    def test_the_automated_set_is_a_positive_list(self):
        """Adding an origin must force a decision, not inherit deletability."""
        assert set(AUTOMATED_ORIGINS) == {AGENT, CONSOLIDATION}


@pytest.fixture
async def wired(pg, provider, cache, monkeypatch):
    await _cleanup(pg)
    monkeypatch.setattr(server, "_pg", pg)
    monkeypatch.setattr(server, "_provider", provider)
    monkeypatch.setattr(server, "_cache", cache)
    monkeypatch.setattr(server, "_config", SimpleNamespace(default_tenant=TENANT))
    yield pg
    await _cleanup(pg)


async def _cleanup(pg):
    await pg.execute(
        "DELETE FROM memory_entities WHERE memory_id IN (SELECT id FROM memories WHERE tenant = %s);", (TENANT,)
    )
    await pg.execute("DELETE FROM memories WHERE tenant = %s;", (TENANT,))


async def _origin_of(pg, content: str) -> str:
    rows = await pg.execute("SELECT origin FROM memories WHERE tenant = %s AND content = %s;", (TENANT, content))
    return rows[0]["origin"]


async def _id_of(pg, content: str) -> str:
    rows = await pg.execute("SELECT id FROM memories WHERE tenant = %s AND content = %s;", (TENANT, content))
    return str(rows[0]["id"])


class TestOriginIsRecordedAtWriteTime:
    async def test_an_unmarked_write_is_human(self, wired):
        await server.remember("unmarked", tenant=TENANT)

        assert await _origin_of(wired, "unmarked") == HUMAN

    @pytest.mark.parametrize("origin", ORIGINS)
    async def test_a_declared_origin_is_stored_verbatim(self, wired, origin):
        await server.remember(f"declared {origin}", tenant=TENANT, origin=origin)

        assert await _origin_of(wired, f"declared {origin}") == origin

    async def test_an_unknown_origin_is_refused(self, wired):
        with pytest.raises(ToolError, match="accepted origins are"):
            await server.remember("never stored", tenant=TENANT, origin="bot")

    async def test_a_refused_origin_stores_nothing(self, wired):
        with pytest.raises(ToolError):
            await server.remember("never stored", tenant=TENANT, origin="bot")

        rows = await wired.execute("SELECT id FROM memories WHERE content = %s;", ("never stored",))
        assert rows == []

    async def test_origin_is_never_recomputed_on_update(self, wired):
        """Set once by the writer; nothing later reconstructs it."""
        await server.remember("stable origin", tenant=TENANT, origin=AGENT)
        memory_id = await _id_of(wired, "stable origin")

        await server.update_memory(memory_id, summary="rewritten by someone else")

        assert await _origin_of(wired, "stable origin") == AGENT


class TestTheDatabaseEnforcesTheVocabulary:
    async def test_a_raw_write_cannot_invent_an_origin(self, wired, provider):
        """A pruning rule must never meet a fourth origin it has no rule for."""
        embedding = (await provider.embed(["x"]))[0]

        with pytest.raises(Exception, match="memories_origin_allowed"):
            await wired.execute(
                """
                INSERT INTO memories (content, embedding, embedding_dim, type, tenant, depth_layer, origin)
                VALUES ('bypass', %s, %s, 'general', %s, 'working', 'robot');
                """,
                (embedding, provider.dimension, TENANT),
            )

    async def test_existing_rows_are_backfilled_as_human(self, wired, provider):
        """No automated writer existed when they were written, so this is also true."""
        embedding = (await provider.embed(["x"]))[0]
        await wired.execute(
            """
            INSERT INTO memories (content, embedding, embedding_dim, type, tenant, depth_layer)
            VALUES ('no origin column supplied', %s, %s, 'general', %s, 'working');
            """,
            (embedding, provider.dimension, TENANT),
        )

        assert await _origin_of(wired, "no origin column supplied") == HUMAN


class TestForgetProtectsHumanWrites:
    async def test_a_human_memory_is_refused_by_default(self, wired):
        await server.remember("mine", tenant=TENANT)
        memory_id = await _id_of(wired, "mine")

        result = await server.forget(memory_id)

        assert "refused" in result
        assert await _id_of(wired, "mine")

    async def test_the_refusal_explains_the_override_and_the_alternative(self, wired):
        await server.remember("mine", tenant=TENANT)

        result = await server.forget(await _id_of(wired, "mine"))

        assert "allow_human=true" in result
        assert "automated passes should skip it" in result

    async def test_an_agent_memory_is_deleted_without_ceremony(self, wired):
        await server.remember("synthesized", tenant=TENANT, origin=AGENT)
        memory_id = await _id_of(wired, "synthesized")

        result = await server.forget(memory_id)

        assert "soft-deleted" in result
        rows = await wired.execute("SELECT deleted_at FROM memories WHERE id = %s;", (memory_id,))
        assert rows[0]["deleted_at"] is not None

    async def test_a_consolidation_memory_is_also_deletable(self, wired):
        await server.remember("merged", tenant=TENANT, origin=CONSOLIDATION)

        assert "soft-deleted" in await server.forget(await _id_of(wired, "merged"))

    async def test_the_override_deletes_and_says_it_did(self, wired):
        await server.remember("mine to delete", tenant=TENANT)
        memory_id = await _id_of(wired, "mine to delete")

        result = await server.forget(memory_id, allow_human=True)

        assert "deleted by explicit override" in result
        rows = await wired.execute("SELECT deleted_at FROM memories WHERE id = %s;", (memory_id,))
        assert rows[0]["deleted_at"] is not None

    async def test_the_override_is_logged(self, wired, caplog):
        await server.remember("logged deletion", tenant=TENANT)

        with caplog.at_level("WARNING", logger="synapto.server"):
            await server.forget(await _id_of(wired, "logged deletion"), allow_human=True)

        assert any("explicit override" in record.message for record in caplog.records)

    async def test_a_missing_memory_is_reported_not_refused(self, wired):
        result = await server.forget("00000000-0000-0000-0000-000000000000")

        assert "not found" in result

    async def test_an_already_deleted_memory_is_reported_not_refused(self, wired):
        await server.remember("gone", tenant=TENANT, origin=AGENT)
        memory_id = await _id_of(wired, "gone")
        await server.forget(memory_id)

        assert "not found" in await server.forget(memory_id)


class TestOriginIsARecallFilter:
    @pytest.fixture
    async def mixed(self, wired):
        await server.remember("finding from the loop", tenant=TENANT, origin=AGENT)
        await server.remember("rule from the user", tenant=TENANT, origin=HUMAN)
        await server.remember("merged summary", tenant=TENANT, origin=CONSOLIDATION)
        return wired

    async def test_a_loop_reads_back_only_what_it_wrote(self, mixed):
        found = await server.recall("from", tenant=TENANT, origin=AGENT)

        assert "finding from the loop" in found
        assert "rule from the user" not in found

    async def test_no_origin_filter_returns_everything(self, mixed):
        found = await server.recall("from", tenant=TENANT)

        assert "finding from the loop" in found
        assert "rule from the user" in found

    async def test_the_count_respects_the_origin(self, mixed):
        assert await count_memories(mixed, tenant=TENANT, origin=AGENT) == 1
        assert await count_memories(mixed, tenant=TENANT) == 3

    async def test_an_unknown_origin_filter_is_a_tool_error(self, wired):
        with pytest.raises(ToolError, match="accepted origins are"):
            await server.recall("anything", tenant=TENANT, origin="bot")

    async def test_a_refused_origin_filter_costs_no_search(self, wired, monkeypatch):
        called = []

        async def fake_search(*_a, **_k):
            called.append(True)
            return []

        monkeypatch.setattr(server, "hybrid_search", fake_search)

        with pytest.raises(ToolError):
            await server.recall("anything", tenant=TENANT, origin="bot")

        assert called == []

    async def test_origin_composes_with_the_metadata_filter(self, wired):
        await server.remember("agent finding", tenant=TENANT, origin=AGENT, metadata={"failure_class": "x"})
        await server.remember("human finding", tenant=TENANT, origin=HUMAN, metadata={"failure_class": "x"})

        assert await count_memories(wired, tenant=TENANT, metadata_filter={"failure_class": "x"}) == 2
        assert await count_memories(wired, tenant=TENANT, metadata_filter={"failure_class": "x"}, origin=AGENT) == 1


class TestOriginIsVisible:
    async def test_get_memory_renders_it(self, wired):
        await server.remember("visible origin", tenant=TENANT, origin=AGENT)

        rendered = await server.get_memory(await _id_of(wired, "visible origin"))

        assert f"origin: {AGENT}" in rendered

    async def test_the_repository_reads_it_rather_than_deriving_it(self, wired):
        await server.remember("read back", tenant=TENANT, origin=CONSOLIDATION)

        assert await MemoryRepository(wired).get_origin(await _id_of(wired, "read back")) == CONSOLIDATION

    async def test_a_missing_memory_has_no_origin(self, wired):
        assert await MemoryRepository(wired).get_origin("00000000-0000-0000-0000-000000000000") is None
