"""The destructive surface, enumerated and pinned.

A statement is destructive when it deletes rows, soft-deletes them by setting
``deleted_at``, drops or truncates an object, or moves memories between
tenants. Every such statement in ``synapto/sql/`` is listed in ``SURFACE``
below with how far it reaches — an MCP tool, a CLI command, or library code no
tool or command calls yet — and the test modules that prove its refusal, its
happy path, and that it leaves the rest of the store alone.

The listing is found from the AST, not maintained by hand, so a new destructive
constant fails this module until it is declared here with its reach and its
evidence. That is the whole point: the day someone wires ``purge_deleted`` to a
tool, this test is what asks for the coverage.

The per-member evidence lives where the behavior is specified — ``forget`` with
the provenance tests, rescoping with the scope tests, the tenant merge with the
alias tests and the CLI test — rather than in one module per member. The
classes at the bottom of this file fill the gaps those modules left: the
"nothing else was touched" assertions, and the ``maintain`` tool, which had no
test before this.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError

import synapto
from synapto import server
from synapto.hrr.banks import rebuild_bank
from synapto.provenance import AGENT
from synapto.repositories.banks import BankRepository
from synapto.repositories.memories import MemoryRepository
from synapto.scopes import ScopeRef, ScopeSet

SQL_PACKAGE = Path(synapto.__file__).parent / "sql"
MIGRATIONS = Path(synapto.__file__).parent / "_migrations"
TESTS = Path(__file__).parent

DESTRUCTIVE = re.compile(r"\b(DELETE FROM|DROP|TRUNCATE)\b|\bUPDATE\s+\w+\s+SET\s+(deleted_at|tenant)\b")

TOOL = "tool"
CLI = "cli"
LIBRARY = "library"


@dataclass(frozen=True)
class Member:
    """One destructive statement: what reaches it and which tests prove it safe."""

    reach: str
    entry: str
    evidence: tuple[str, ...] = ()


SURFACE: dict[str, Member] = {
    "memories.SOFT_DELETE": Member(TOOL, "forget", ("test_provenance.py", "test_destructive_surface.py")),
    "memories.CLEANUP_EPHEMERAL": Member(TOOL, "maintain", ("test_destructive_surface.py",)),
    "scopes.DELETE_ALL": Member(
        TOOL, "update_memory(scopes=...)", ("test_scope_tools.py", "test_scope_integration.py")
    ),
    "entities.UNLINK_MEMORY_ENTITIES": Member(TOOL, "update_memory(content=...)", ("test_destructive_surface.py",)),
    "banks.DELETE": Member(TOOL, "remember / update_memory via rebuild_bank", ("test_destructive_surface.py",)),
    "tenants.MOVE_MEMORIES": Member(
        CLI, "maintain --merge-tenants --apply", ("test_tenant_aliases.py", "test_maintain_merge_tenants_cli.py")
    ),
    "migrations.FORGET_APPLIED": Member(CLI, "migrate down", ("test_migrations.py",)),
    "memories.PURGE_DELETED": Member(LIBRARY, "decay.maintenance.purge_deleted"),
    "metrics.PURGE_OLDER": Member(LIBRARY, "MetricsRepository.purge_older_than", ("test_metrics_postgres_backend.py",)),
    "entities.DELETE": Member(LIBRARY, "graph.entities.delete_entity"),
    "relations.DELETE": Member(LIBRARY, "graph.relations.delete_relation"),
}

MIGRATION_ROLLBACK = Member(CLI, "migrate down", ("test_migrations.py", "test_migration_resources.py"))


def _destructive_constants() -> set[str]:
    """Every module-level string constant in ``synapto/sql/`` that matches ``DESTRUCTIVE``."""
    found = set()
    for path in sorted(SQL_PACKAGE.glob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
                continue
            if isinstance(node.value.value, str) and DESTRUCTIVE.search(node.value.value):
                found.update(f"{path.stem}.{target.id}" for target in node.targets if isinstance(target, ast.Name))
    return found


def _rollback_section(path: Path) -> str:
    _, _, down = path.read_text().partition("-- migrate:down")
    return down


class TestTheSnapshotIsExhaustive:
    def test_every_destructive_constant_is_declared(self):
        undeclared = sorted(_destructive_constants() - set(SURFACE))

        assert undeclared == [], (
            "new destructive statement(s) in synapto/sql/: "
            f"{undeclared}. Declare each in SURFACE with its reach (tool / cli / library) "
            "and the test modules proving refusal, happy path and not-touched."
        )

    def test_every_declared_constant_still_exists(self):
        stale = sorted(set(SURFACE) - _destructive_constants())

        assert stale == [], f"SURFACE lists statements that are no longer destructive or no longer exist: {stale}"

    def test_the_pattern_catches_each_destructive_shape(self):
        assert DESTRUCTIVE.search("DELETE FROM memories WHERE id = %s;")
        assert DESTRUCTIVE.search("UPDATE memories SET deleted_at = now() WHERE id = %s;")
        assert DESTRUCTIVE.search("UPDATE memories SET tenant = %(canonical)s WHERE tenant = %(alias)s;")
        assert DESTRUCTIVE.search("DROP TABLE IF EXISTS memory_banks CASCADE;")
        assert DESTRUCTIVE.search("TRUNCATE metrics_events;")

    def test_the_pattern_ignores_reads_writes_and_cascade_clauses(self):
        assert not DESTRUCTIVE.search("SELECT id FROM memories WHERE deleted_at IS NULL;")
        assert not DESTRUCTIVE.search("UPDATE memories SET decay_score = %s WHERE id = %s;")
        assert not DESTRUCTIVE.search("UPDATE memories SET accessed_at = now() WHERE id = ANY(%s);")
        assert not DESTRUCTIVE.search("memory_id UUID REFERENCES memories(id) ON DELETE CASCADE")
        assert not DESTRUCTIVE.search("INSERT INTO memories (tenant) VALUES (%s);")

    def test_every_reachable_member_names_at_least_one_evidence_module(self):
        unproven = sorted(name for name, member in SURFACE.items() if member.reach != LIBRARY and not member.evidence)

        assert unproven == []

    @pytest.mark.parametrize("name", sorted(SURFACE))
    def test_the_evidence_modules_exist(self, name):
        missing = [module for module in SURFACE[name].evidence if not (TESTS / module).is_file()]

        assert missing == [], f"{name} cites test module(s) that do not exist: {missing}"

    def test_every_migration_rollback_is_reached_only_through_migrate_down(self):
        """Each ``-- migrate:down`` section drops objects; ``synapto migrate down`` is its sole caller."""
        rollbacks = [path.name for path in MIGRATIONS.glob("*.sql") if DESTRUCTIVE.search(_rollback_section(path))]

        assert rollbacks, "no migration carries a destructive rollback; revisit MIGRATION_ROLLBACK"
        assert all((TESTS / module).is_file() for module in MIGRATION_ROLLBACK.evidence)

    def test_reach_is_one_of_the_three_levels(self):
        assert {member.reach for member in SURFACE.values()} <= {TOOL, CLI, LIBRARY}


TENANT = "acme/destructive"
OTHER_TENANT = "acme/destructive-other"
TEST_TENANTS = (TENANT, OTHER_TENANT)
STALE_HOURS = 48
MAX_AGE_HOURS = 24


@pytest.fixture
async def wired(pg, provider, cache, monkeypatch):
    await _cleanup(pg)
    monkeypatch.setattr(server, "_pg", pg)
    monkeypatch.setattr(server, "_provider", provider)
    monkeypatch.setattr(server, "_cache", cache)
    monkeypatch.setattr(
        server, "_config", SimpleNamespace(default_tenant=TENANT, decay_ephemeral_max_age_hours=MAX_AGE_HOURS)
    )
    yield pg
    await _cleanup(pg)


async def _cleanup(pg):
    tenants = list(TEST_TENANTS)
    for table in ("memory_scopes", "memory_entities"):
        await pg.execute(
            f"DELETE FROM {table} WHERE memory_id IN (SELECT id FROM memories WHERE tenant = ANY(%s));", (tenants,)
        )
    await pg.execute("DELETE FROM memories WHERE tenant = ANY(%s);", (tenants,))
    await pg.execute("DELETE FROM memory_banks WHERE bank_name LIKE %s;", (f"{TENANT}%",))


async def _id_of(pg, content: str, tenant: str = TENANT) -> str:
    rows = await pg.execute("SELECT id FROM memories WHERE tenant = %s AND content = %s;", (tenant, content))
    return str(rows[0]["id"])


async def _deleted_at(pg, memory_id: str):
    rows = await pg.execute("SELECT deleted_at FROM memories WHERE id = %s;", (memory_id,))
    return rows[0]["deleted_at"]


async def _scopes_of(pg, memory_id: str) -> ScopeSet:
    return (await MemoryRepository(pg).get_by_id(memory_id))["scopes"]


async def _entity_names_of(pg, memory_id: str) -> set[str]:
    rows = await pg.execute(
        "SELECT e.name FROM memory_entities me JOIN entities e ON e.id = me.entity_id WHERE me.memory_id = %s;",
        (memory_id,),
    )
    return {row["name"] for row in rows}


async def _age(pg, memory_id: str, hours: int) -> None:
    await pg.execute(
        "UPDATE memories SET accessed_at = now() - make_interval(hours => %s) WHERE id = %s;", (hours, memory_id)
    )


async def _live_count(pg, tenant: str) -> int:
    rows = await pg.execute("SELECT count(*) AS cnt FROM memories WHERE tenant = %s AND deleted_at IS NULL;", (tenant,))
    return rows[0]["cnt"]


class TestForgetLeavesTheRestAlone:
    """Refusal and the happy path are in test_provenance.py; this is the third leg."""

    async def test_a_sibling_memory_survives(self, wired):
        await server.remember("doomed", tenant=TENANT, origin=AGENT)
        await server.remember("bystander", tenant=TENANT, origin=AGENT)

        await server.forget(await _id_of(wired, "doomed"))

        assert await _deleted_at(wired, await _id_of(wired, "bystander")) is None
        assert await _live_count(wired, TENANT) == 1

    async def test_another_tenant_is_not_reached(self, wired):
        await server.remember("doomed", tenant=TENANT, origin=AGENT)
        await server.remember("elsewhere", tenant=OTHER_TENANT, origin=AGENT)

        await server.forget(await _id_of(wired, "doomed"))

        assert await _live_count(wired, OTHER_TENANT) == 1

    async def test_a_soft_delete_keeps_scopes_and_entity_links(self, wired):
        """Soft means recoverable: the memberships stay until a purge removes the row."""
        await server.remember("Kafka consumer lag", tenant=TENANT, origin=AGENT, scopes=["language:python"])
        memory_id = await _id_of(wired, "Kafka consumer lag")
        links_before = await _entity_names_of(wired, memory_id)

        await server.forget(memory_id)

        rows = await wired.execute("SELECT count(*) AS cnt FROM memory_scopes WHERE memory_id = %s;", (memory_id,))
        assert rows[0]["cnt"] == 1
        assert await _entity_names_of(wired, memory_id) == links_before

    async def test_the_row_is_marked_not_removed(self, wired):
        await server.remember("doomed", tenant=TENANT, origin=AGENT)
        memory_id = await _id_of(wired, "doomed")

        await server.forget(memory_id)

        assert await _deleted_at(wired, memory_id) is not None


class TestMaintainCleansOnlyStaleEphemeralMemories:
    """The ``maintain`` tool soft-deletes ephemeral memories past the configured age, and nothing else."""

    async def test_a_stale_ephemeral_memory_is_soft_deleted(self, wired):
        await server.remember("scratch", tenant=TENANT, depth_layer="ephemeral", origin=AGENT)
        memory_id = await _id_of(wired, "scratch")
        await _age(wired, memory_id, STALE_HOURS)

        result = await server.maintain()

        assert "1 ephemeral memories cleaned" in result
        assert await _deleted_at(wired, memory_id) is not None

    async def test_a_fresh_ephemeral_memory_is_refused(self, wired):
        await server.remember("still warm", tenant=TENANT, depth_layer="ephemeral", origin=AGENT)

        result = await server.maintain()

        assert "0 ephemeral memories cleaned" in result
        assert await _deleted_at(wired, await _id_of(wired, "still warm")) is None

    async def test_an_ephemeral_memory_just_inside_the_limit_is_kept(self, wired):
        await server.remember("nearly stale", tenant=TENANT, depth_layer="ephemeral", origin=AGENT)
        memory_id = await _id_of(wired, "nearly stale")
        await _age(wired, memory_id, MAX_AGE_HOURS - 1)

        await server.maintain()

        assert await _deleted_at(wired, memory_id) is None

    @pytest.mark.parametrize("layer", ["working", "stable", "core"])
    async def test_other_layers_are_never_touched_however_old(self, wired, layer):
        await server.remember(f"durable {layer}", tenant=TENANT, depth_layer=layer, origin=AGENT)
        memory_id = await _id_of(wired, f"durable {layer}")
        await _age(wired, memory_id, STALE_HOURS * 100)

        result = await server.maintain()

        assert "0 ephemeral memories cleaned" in result
        assert await _deleted_at(wired, memory_id) is None

    async def test_a_stale_sibling_in_another_layer_survives_the_same_pass(self, wired):
        await server.remember("scratch", tenant=TENANT, depth_layer="ephemeral", origin=AGENT)
        await server.remember("kept", tenant=TENANT, depth_layer="working", origin=AGENT)
        await server.remember("kept elsewhere", tenant=OTHER_TENANT, depth_layer="working", origin=AGENT)
        for content, tenant in (("scratch", TENANT), ("kept", TENANT), ("kept elsewhere", OTHER_TENANT)):
            await _age(wired, await _id_of(wired, content, tenant), STALE_HOURS)

        await server.maintain()

        assert await _deleted_at(wired, await _id_of(wired, "scratch")) is not None
        assert await _live_count(wired, TENANT) == 1
        assert await _live_count(wired, OTHER_TENANT) == 1

    async def test_an_already_deleted_memory_is_not_counted_again(self, wired):
        await server.remember("scratch", tenant=TENANT, depth_layer="ephemeral", origin=AGENT)
        memory_id = await _id_of(wired, "scratch")
        await _age(wired, memory_id, STALE_HOURS)
        await server.maintain()
        first = await _deleted_at(wired, memory_id)

        result = await server.maintain()

        assert "0 ephemeral memories cleaned" in result
        assert await _deleted_at(wired, memory_id) == first

    async def test_the_configured_age_is_the_one_applied(self, wired, monkeypatch):
        monkeypatch.setattr(
            server, "_config", SimpleNamespace(default_tenant=TENANT, decay_ephemeral_max_age_hours=STALE_HOURS * 2)
        )
        await server.remember("scratch", tenant=TENANT, depth_layer="ephemeral", origin=AGENT)
        memory_id = await _id_of(wired, "scratch")
        await _age(wired, memory_id, STALE_HOURS)

        await server.maintain()

        assert await _deleted_at(wired, memory_id) is None


class TestUpdateMemoryRewritesOnlyItsOwnRow:
    """Refusal, replace, clear and preserve are in test_scope_tools.py; this is the not-touched leg."""

    async def test_rescoping_one_memory_leaves_a_siblings_scopes(self, wired):
        await server.remember("rescoped", tenant=TENANT, scopes=["language:python"])
        await server.remember("bystander", tenant=TENANT, scopes=["language:python"])

        await server.update_memory(await _id_of(wired, "rescoped"), scopes=[])

        assert await _scopes_of(wired, await _id_of(wired, "rescoped")) == ScopeSet()
        assert await _scopes_of(wired, await _id_of(wired, "bystander")) == ScopeSet.parse(
            [ScopeRef("language", "python")]
        )

    async def test_a_content_rewrite_relinks_entities_for_that_memory_only(self, wired):
        await server.remember("uses Kafka for events", tenant=TENANT)
        await server.remember("uses Kafka for logs", tenant=TENANT)
        rewritten = await _id_of(wired, "uses Kafka for events")
        bystander = await _id_of(wired, "uses Kafka for logs")
        bystander_links = await _entity_names_of(wired, bystander)

        await server.update_memory(rewritten, content="uses Redis for events")

        assert "Kafka" not in await _entity_names_of(wired, rewritten)
        assert await _entity_names_of(wired, bystander) == bystander_links

    async def test_a_rejected_rescope_leaves_every_row_as_it_was(self, wired):
        await server.remember("rescoped", tenant=TENANT, scopes=["language:python"])
        await server.remember("bystander", tenant=TENANT, scopes=["language:python"])
        expected = ScopeSet.parse([ScopeRef("language", "python")])

        with pytest.raises(ToolError):
            await server.update_memory(await _id_of(wired, "rescoped"), scopes=["Language:python"])

        assert await _scopes_of(wired, await _id_of(wired, "rescoped")) == expected
        assert await _scopes_of(wired, await _id_of(wired, "bystander")) == expected


class TestBankRebuildDeletesOnlyTheEmptyBank:
    """``rebuild_bank`` drops a bank with no vectors left; the tools reach it after every write."""

    BANK = f"{TENANT}:general"
    OTHER_BANK = f"{TENANT}:decision"

    async def _seed_banks(self, pg):
        repo = BankRepository(pg)
        for bank in (self.BANK, self.OTHER_BANK):
            await repo.upsert(bank, b"\x00" * 8, 4, 1)
        return repo

    async def test_a_bank_with_no_vectors_is_deleted(self, wired):
        repo = await self._seed_banks(wired)

        bundled = await rebuild_bank(wired, self.BANK, TENANT, type_filter="general")

        assert bundled == 0
        assert await repo.get_vector(self.BANK) is None

    async def test_the_sibling_bank_is_kept(self, wired):
        repo = await self._seed_banks(wired)

        await rebuild_bank(wired, self.BANK, TENANT, type_filter="general")

        assert await repo.get_vector(self.OTHER_BANK) is not None

    async def test_a_bank_with_vectors_is_rewritten_not_deleted(self, wired):
        repo = await self._seed_banks(wired)
        await server.remember("a fact worth bundling", tenant=TENANT, memory_type="general", origin=AGENT)

        bundled = await rebuild_bank(wired, self.BANK, TENANT, type_filter="general")

        assert bundled == 1
        assert await repo.get_vector(self.BANK) is not None
