"""``synapto maintain --merge-tenants`` against a real database, ``--apply`` included.

Until now the apply path had only its repository test; the command that a
human actually runs — planning from ``count_by_tenant``, splitting the plan
into actionable and held groups, then calling ``merge`` per member — was
exercised only in dry-run form. These tests run the click command through
``CliRunner`` with a real ``PostgresClient`` on the disposable test database,
because the property worth proving is end to end: what the command prints as
its plan is exactly what it moves, and nothing outside that plan changes.

The command plans over every tenant in the database, not only the ones a test
inserted, so the assertions about "untouched" are made against counts recorded
before each run, and the tenant names are chosen so no other module's leftovers
can fold into them.

The command's ``asyncio.run`` cannot start inside the test's running loop, so
``CliRunner.invoke`` is dispatched to a worker thread.

What the first apply test found: the planner's highest-confidence group,
spellings "identical once case and '_' are normalized", cannot be applied when
the difference is case or an underscore in the owner segment. Those spellings
are non-canonical by definition, and ``TenantAliasRepository.merge`` validates
the alias as canonical before moving anything, so the command dies with an
``InvalidTenantError`` traceback after the plan is printed — and after any
earlier group in the same run has already been merged. ``TestKnownGap`` pins
that as a strict expected failure so the fix flips it rather than silently
landing.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from synapto.cli import main
from synapto.repositories.memories import MemoryRepository
from synapto.repositories.tenants import TenantAliasRepository
from synapto.scopes import ScopeRef, ScopeSet
from synapto.tenants import InvalidTenantError
from tests.db_guard import resolve_test_dsn

CANONICAL = "acme-mergecli/svc-api"
NAME_VARIANT = "acme-mergecli/svc_api"
CASE_VARIANT = "ACME-MergeCLI/svc-api"
UNQUALIFIED = "svc-api"
ONE_OWNER = "acme-mergecli/dup"
OTHER_OWNER = "beta-mergecli/dup"
CONTROL = "acme-mergecli/control"

TEST_TENANTS = (CANONICAL, NAME_VARIANT, CASE_VARIANT, UNQUALIFIED, ONE_OWNER, OTHER_OWNER, CONTROL)
SCOPES = ScopeSet.parse([ScopeRef("language", "python")])


@pytest.fixture
async def store(pg, provider, monkeypatch):
    """A clean slate for the test tenants, the CLI wired to the test database."""
    await _cleanup(pg)
    config = SimpleNamespace(pg_dsn=resolve_test_dsn(), default_tenant=CANONICAL)
    monkeypatch.setattr("synapto.config.load_config", lambda: config)
    yield _Store(pg, provider)
    await _cleanup(pg)


class _Store:
    def __init__(self, pg, provider):
        self.pg = pg
        self.provider = provider

    async def memory(self, tenant: str, content: str = "content", *, scopes: ScopeSet | None = None) -> str:
        embedding = (await self.provider.embed([content]))[0]
        memory_id = await MemoryRepository(self.pg).create(
            content=content,
            embedding=embedding,
            embedding_dim=self.provider.dimension,
            memory_type="general",
            tenant=tenant,
            depth_layer="working",
            scopes=scopes,
        )
        return str(memory_id)

    async def memories(self, tenant: str, count: int) -> None:
        for _ in range(count):
            await self.memory(tenant)

    async def count(self, tenant: str) -> int:
        rows = await self.pg.execute("SELECT count(*) AS cnt FROM memories WHERE tenant = %s;", (tenant,))
        return rows[0]["cnt"]

    async def counts(self) -> dict[str, int]:
        rows = await MemoryRepository(self.pg).count_by_tenant()
        return {row["tenant"]: row["cnt"] for row in rows}

    async def tenant_of(self, memory_id: str) -> str:
        rows = await self.pg.execute("SELECT tenant FROM memories WHERE id = %s;", (memory_id,))
        return rows[0]["tenant"]

    async def scopes_of(self, memory_id: str) -> ScopeSet:
        return (await MemoryRepository(self.pg).get_by_id(memory_id))["scopes"]

    async def aliases(self) -> dict[str, str]:
        rows = await TenantAliasRepository(self.pg).list_aliases()
        return {row["alias"]: row["canonical"] for row in rows if row["alias"] in TEST_TENANTS}


async def _cleanup(pg):
    tenants = list(TEST_TENANTS)
    await pg.execute("DELETE FROM tenant_aliases WHERE alias = ANY(%s) OR canonical = ANY(%s);", (tenants, tenants))
    await pg.execute(
        "DELETE FROM memory_scopes WHERE memory_id IN (SELECT id FROM memories WHERE tenant = ANY(%s));", (tenants,)
    )
    await pg.execute("DELETE FROM memories WHERE tenant = ANY(%s);", (tenants,))


async def _invoke(*args: str):
    return await asyncio.to_thread(CliRunner().invoke, main, ["maintain", "--merge-tenants", *args])


class TestTheDefaultIsADryRun:
    async def test_without_a_flag_nothing_moves(self, store):
        moved_from = await store.memory(NAME_VARIANT)
        await store.memories(CANONICAL, 2)

        result = await _invoke()

        assert result.exit_code == 0, result.output
        assert "dry run — nothing changed. --apply would move 1 memories." in result.output
        assert await store.tenant_of(moved_from) == NAME_VARIANT
        assert await store.aliases() == {}

    async def test_dry_run_is_explicit_too(self, store):
        moved_from = await store.memory(NAME_VARIANT)
        await store.memories(CANONICAL, 2)

        result = await _invoke("--dry-run")

        assert result.exit_code == 0, result.output
        assert await store.tenant_of(moved_from) == NAME_VARIANT

    async def test_the_operation_must_be_named(self):
        result = await asyncio.to_thread(CliRunner().invoke, main, ["maintain"])

        assert result.exit_code == 2
        assert "select an operation" in result.output


class TestApplyMovesExactlyThePlan:
    async def test_a_name_variant_folds_into_the_most_populated_spelling(self, store):
        await store.memories(CANONICAL, 2)
        folded = await store.memory(NAME_VARIANT)

        result = await _invoke("--apply")

        assert result.exit_code == 0, result.output
        assert "[merge] -> acme-mergecli/svc-api   (identical once case and '_' are normalized)" in result.output
        assert "merged 1 memories into 1 canonical tenant(s)" in result.output
        assert await store.tenant_of(folded) == CANONICAL
        assert await store.count(NAME_VARIANT) == 0
        assert await store.count(CANONICAL) == 3

    async def test_the_alias_is_recorded_so_the_old_spelling_still_resolves(self, store):
        await store.memories(CANONICAL, 2)
        await store.memory(NAME_VARIANT)

        await _invoke("--apply")

        assert await store.aliases() == {NAME_VARIANT: CANONICAL}
        assert await TenantAliasRepository(store.pg).resolve(NAME_VARIANT) == CANONICAL

    async def test_an_unqualified_spelling_follows_the_single_qualified_one(self, store):
        """One ``owner/name`` and a bare ``name`` is a review-grade group; the command applies it."""
        await store.memories(UNQUALIFIED, 3)
        await store.memory(CANONICAL)

        result = await _invoke("--apply")

        assert result.exit_code == 0, result.output
        assert "[check] -> acme-mergecli/svc-api" in result.output
        assert await store.count(UNQUALIFIED) == 0
        assert await store.count(CANONICAL) == 4
        assert await store.aliases() == {UNQUALIFIED: CANONICAL}

    async def test_scopes_travel_with_the_memory(self, store):
        await store.memories(CANONICAL, 2)
        scoped = await store.memory(NAME_VARIANT, "scoped", scopes=SCOPES)

        await _invoke("--apply")

        assert await store.tenant_of(scoped) == CANONICAL
        assert await store.scopes_of(scoped) == SCOPES

    async def test_the_moved_count_matches_the_dry_run_promise(self, store):
        await store.memories(CANONICAL, 2)
        await store.memories(NAME_VARIANT, 2)
        promised = await _invoke()

        applied = await _invoke("--apply")

        assert "--apply would move 2 memories" in promised.output
        assert "merged 2 memories" in applied.output


class TestApplyRefusesToGuess:
    async def test_two_owners_claiming_one_name_are_held(self, store):
        one = await store.memory(ONE_OWNER)
        other = await store.memory(OTHER_OWNER)

        result = await _invoke("--apply")

        assert result.exit_code == 0, result.output
        assert "[STOP ] -> (undecided)" in result.output
        assert "need a human decision and will not be touched" in result.output
        assert "nothing can be merged without a decision." in result.output
        assert await store.tenant_of(one) == ONE_OWNER
        assert await store.tenant_of(other) == OTHER_OWNER
        assert await store.aliases() == {}

    async def test_a_held_group_does_not_block_an_actionable_one(self, store):
        await store.memories(CANONICAL, 2)
        folded = await store.memory(NAME_VARIANT)
        one = await store.memory(ONE_OWNER)
        other = await store.memory(OTHER_OWNER)

        result = await _invoke("--apply")

        assert "merged 1 memories into 1 canonical tenant(s)" in result.output
        assert await store.tenant_of(folded) == CANONICAL
        assert await store.tenant_of(one) == ONE_OWNER
        assert await store.tenant_of(other) == OTHER_OWNER

    async def test_a_second_apply_finds_nothing_left(self, store):
        await store.memories(CANONICAL, 2)
        await store.memory(NAME_VARIANT)
        await _invoke("--apply")

        result = await _invoke("--apply")

        assert result.exit_code == 0, result.output
        assert "merged 0 memories" not in result.output
        assert "nothing can be merged without a decision." in result.output
        assert await store.aliases() == {NAME_VARIANT: CANONICAL}


class TestApplyLeavesTheRestOfTheStoreAlone:
    async def test_a_tenant_with_no_sibling_spelling_is_unchanged(self, store):
        control = await store.memory(CONTROL, "control", scopes=SCOPES)
        await store.memories(CANONICAL, 2)
        await store.memory(NAME_VARIANT)

        result = await _invoke("--apply")

        assert CONTROL in result.output.split("unchanged")[1]
        assert await store.tenant_of(control) == CONTROL
        assert await store.scopes_of(control) == SCOPES

    async def test_tenants_outside_the_plan_keep_their_counts(self, store):
        await store.memories(CANONICAL, 2)
        await store.memory(NAME_VARIANT)
        await store.memory(CONTROL)
        before = await store.counts()
        planned = {CANONICAL, NAME_VARIANT}

        await _invoke("--apply")

        after = await store.counts()
        assert {t: n for t, n in after.items() if t not in planned} == {
            t: n for t, n in before.items() if t not in planned
        }

    async def test_the_total_number_of_memories_is_conserved(self, store):
        await store.memories(CANONICAL, 2)
        await store.memories(NAME_VARIANT, 2)
        await store.memory(CONTROL)
        before = sum((await store.counts()).values())

        await _invoke("--apply")

        assert sum((await store.counts()).values()) == before


class TestKnownGap:
    """A case variant is planned as an exact merge and then refused by the alias validation."""

    async def test_the_plan_proposes_the_case_variant(self, store):
        await store.memories(CANONICAL, 2)
        await store.memory(CASE_VARIANT)

        result = await _invoke()

        assert "[merge] -> acme-mergecli/svc-api" in result.output
        assert "--apply would move 1 memories" in result.output

    @pytest.mark.xfail(strict=True, raises=InvalidTenantError, reason="merge validates the alias as canonical")
    async def test_applying_it_moves_the_memories(self, store):
        await store.memories(CANONICAL, 2)
        folded = await store.memory(CASE_VARIANT)

        result = await _invoke("--apply")

        if result.exception:
            raise result.exception
        assert await store.tenant_of(folded) == CANONICAL

    async def test_the_refused_group_is_left_exactly_as_it_was(self, store):
        await store.memories(CANONICAL, 2)
        folded = await store.memory(CASE_VARIANT)

        result = await _invoke("--apply")

        assert result.exit_code != 0
        assert await store.tenant_of(folded) == CASE_VARIANT
        assert await store.count(CANONICAL) == 2
        assert await store.aliases() == {}
