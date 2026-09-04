"""Tests for the tenant alias table and the merge it records.

These run against PostgreSQL because the two things worth proving are storage
guarantees: that the one-hop invariant holds under the lock rather than only in
Python, and that moving memories and recording the alias cannot half-happen.
"""

from __future__ import annotations

import pytest

from synapto.repositories.memories import MemoryRepository
from synapto.repositories.tenants import TenantAliasError, TenantAliasRepository
from synapto.tenants import InvalidTenantError

CANONICAL = "acme/api"
ALIAS = "api"
OTHER = "acme/web"

TEST_TENANTS = (CANONICAL, ALIAS, OTHER, "acme/other", "legacy-api")


@pytest.fixture
async def aliases(pg):
    await _cleanup(pg)
    yield TenantAliasRepository(pg)
    await _cleanup(pg)


async def _cleanup(pg):
    await pg.execute("DELETE FROM tenant_aliases WHERE alias = ANY(%s) OR canonical = ANY(%s);",
                     (list(TEST_TENANTS), list(TEST_TENANTS)))
    await pg.execute("DELETE FROM memories WHERE tenant = ANY(%s);", (list(TEST_TENANTS),))


async def _memory(pg, provider, tenant: str) -> None:
    embedding = (await provider.embed(["content"]))[0]
    await MemoryRepository(pg).create(
        content="content",
        embedding=embedding,
        embedding_dim=provider.dimension,
        memory_type="general",
        tenant=tenant,
        depth_layer="working",
    )


async def _count(pg, tenant: str) -> int:
    rows = await pg.execute("SELECT count(*) AS cnt FROM memories WHERE tenant = %s;", (tenant,))
    return rows[0]["cnt"]


class TestResolution:
    async def test_an_unmerged_tenant_resolves_to_itself(self, aliases):
        assert await aliases.resolve(CANONICAL) == CANONICAL

    async def test_a_registered_alias_resolves_to_its_canonical(self, aliases):
        await aliases.register(ALIAS, CANONICAL)

        assert await aliases.resolve(ALIAS) == CANONICAL

    async def test_resolution_is_not_symmetric(self, aliases):
        """The canonical must not resolve back to the alias."""
        await aliases.register(ALIAS, CANONICAL)

        assert await aliases.resolve(CANONICAL) == CANONICAL

    async def test_registering_again_repoints_rather_than_duplicating(self, aliases):
        await aliases.register(ALIAS, CANONICAL)
        await aliases.register(ALIAS, OTHER)

        assert await aliases.resolve(ALIAS) == OTHER
        assert len(await aliases.list_aliases()) == 1


class TestTheOneHopInvariant:
    async def test_pointing_at_an_alias_is_refused(self, aliases):
        await aliases.register(ALIAS, CANONICAL)

        with pytest.raises(TenantAliasError, match="is itself an alias"):
            await aliases.register("legacy-api", ALIAS)

    async def test_the_refusal_names_the_end_of_the_chain(self, aliases):
        await aliases.register(ALIAS, CANONICAL)

        with pytest.raises(TenantAliasError, match=f"point 'legacy-api' at '{CANONICAL}'"):
            await aliases.register("legacy-api", ALIAS)

    async def test_making_an_existing_canonical_into_an_alias_is_refused(self, aliases):
        await aliases.register(ALIAS, CANONICAL)

        with pytest.raises(TenantAliasError, match="already the canonical tenant"):
            await aliases.register(CANONICAL, OTHER)

    async def test_a_refused_chain_leaves_the_table_unchanged(self, aliases):
        await aliases.register(ALIAS, CANONICAL)

        with pytest.raises(TenantAliasError):
            await aliases.register("legacy-api", ALIAS)

        assert [row["alias"] for row in await aliases.list_aliases()] == [ALIAS]

    async def test_self_reference_is_refused(self, aliases):
        with pytest.raises(TenantAliasError, match="cannot be an alias of itself"):
            await aliases.register(CANONICAL, CANONICAL)


class TestNonCanonicalValuesNeverReachStorage:
    @pytest.mark.parametrize("bad", ["Acme/API", " api ", "acme/api/extra"])
    async def test_a_non_canonical_alias_is_refused(self, aliases, bad):
        with pytest.raises(InvalidTenantError, match="alias"):
            await aliases.register(bad, CANONICAL)

    @pytest.mark.parametrize("bad", ["Acme/API", "acme//api"])
    async def test_a_non_canonical_canonical_is_refused(self, aliases, bad):
        with pytest.raises(InvalidTenantError, match="canonical"):
            await aliases.register(ALIAS, bad)

    async def test_the_table_refuses_what_bypasses_the_repository(self, pg, aliases):
        """The row-local half of the contract, proven against the database."""
        with pytest.raises(Exception, match="tenant_aliases_alias_grammar"):
            await pg.execute("INSERT INTO tenant_aliases (alias, canonical) VALUES (%s, %s);", ("Bad Alias", CANONICAL))

    async def test_the_table_refuses_a_self_reference(self, pg, aliases):
        with pytest.raises(Exception, match="tenant_aliases_no_self_reference"):
            await pg.execute("INSERT INTO tenant_aliases (alias, canonical) VALUES (%s, %s);", (CANONICAL, CANONICAL))


class TestMerge:
    async def test_memories_move_and_the_alias_is_recorded(self, pg, provider, aliases):
        await _memory(pg, provider, ALIAS)
        await _memory(pg, provider, ALIAS)
        await _memory(pg, provider, CANONICAL)

        moved = await aliases.merge(ALIAS, CANONICAL)

        assert moved == 2
        assert await _count(pg, ALIAS) == 0
        assert await _count(pg, CANONICAL) == 3
        assert await aliases.resolve(ALIAS) == CANONICAL

    async def test_merging_an_empty_tenant_still_records_the_alias(self, pg, aliases):
        moved = await aliases.merge(ALIAS, CANONICAL)

        assert moved == 0
        assert await aliases.resolve(ALIAS) == CANONICAL

    async def test_a_refused_merge_moves_nothing(self, pg, provider, aliases):
        """The move and the alias share a transaction, so a late refusal undoes both."""
        await aliases.register(ALIAS, CANONICAL)
        await _memory(pg, provider, "legacy-api")

        with pytest.raises(TenantAliasError):
            await aliases.merge("legacy-api", ALIAS)

        assert await _count(pg, "legacy-api") == 1
        assert await _count(pg, ALIAS) == 0

    async def test_merging_a_tenant_into_itself_is_refused(self, aliases):
        with pytest.raises(TenantAliasError, match="cannot be an alias of itself"):
            await aliases.merge(CANONICAL, CANONICAL)


class TestListing:
    async def test_mappings_come_back_ordered(self, aliases):
        await aliases.register("legacy-api", CANONICAL)
        await aliases.register(ALIAS, CANONICAL)

        rows = await aliases.list_aliases()

        assert [(r["alias"], r["canonical"]) for r in rows] == [
            (ALIAS, CANONICAL),
            ("legacy-api", CANONICAL),
        ]

    async def test_an_empty_table_lists_nothing(self, aliases):
        assert await aliases.list_aliases() == []
