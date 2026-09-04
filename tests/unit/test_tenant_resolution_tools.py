"""Tests that the MCP tools actually resolve tenants the way the module says.

`test_tenants.py` proves the resolution rules and `test_tenant_aliases.py`
proves the storage guarantees. What is left, and what these cover, is the wiring
in between: that a tool derives when nothing is supplied, refuses a
non-canonical override with a usable message, and follows an alias so a folded
tenant is not silently empty.

Recall is the tool under test throughout because it is the read whose failure
mode is invisible — it returns fewer results rather than an error.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError

from synapto import server, tenants
from synapto.repositories.tenants import TenantAliasRepository

CANONICAL = "acme/api"
ALIAS = "legacy-api"
DERIVED = "acme/derived"


@pytest.fixture(autouse=True)
def _isolated_cache():
    tenants.clear_tenant_cache()
    yield
    tenants.clear_tenant_cache()


@pytest.fixture
async def wired(pg, provider, cache, monkeypatch):
    """A server whose globals point at the test database."""
    await pg.execute("DELETE FROM tenant_aliases WHERE alias = ANY(%s);", ([ALIAS, CANONICAL, DERIVED],))
    monkeypatch.setattr(server, "_pg", pg)
    monkeypatch.setattr(server, "_provider", provider)
    monkeypatch.setattr(server, "_cache", cache)
    monkeypatch.setattr(server, "_config", SimpleNamespace(default_tenant="default"))
    yield pg
    await pg.execute("DELETE FROM tenant_aliases WHERE alias = ANY(%s);", ([ALIAS, CANONICAL, DERIVED],))


def _capture_tenant(monkeypatch) -> list[str]:
    """Record the tenant hybrid_search is asked for, without running a search."""
    seen: list[str] = []

    async def fake_search(*_args, tenant, **_kwargs):
        seen.append(tenant)
        return []

    monkeypatch.setattr(server, "hybrid_search", fake_search)
    return seen


def _derives(monkeypatch, remote: str) -> None:
    monkeypatch.setattr(tenants, "_run_git", lambda _command: remote)


class TestExplicitOverrides:
    async def test_a_canonical_tenant_is_used_as_given(self, wired, monkeypatch):
        seen = _capture_tenant(monkeypatch)

        await server.recall("anything", tenant=CANONICAL)

        assert seen == [CANONICAL]

    async def test_a_non_canonical_tenant_is_refused(self, wired):
        with pytest.raises(ToolError, match="not canonical"):
            await server.recall("anything", tenant="Acme/API")

    async def test_the_refusal_names_the_canonical_spelling(self, wired):
        with pytest.raises(ToolError, match="did you mean 'acme/api'"):
            await server.recall("anything", tenant="Acme/API")

    async def test_a_refused_tenant_never_reaches_the_search(self, wired, monkeypatch):
        seen = _capture_tenant(monkeypatch)

        with pytest.raises(ToolError):
            await server.recall("anything", tenant="  acme/api  ")

        assert seen == []

    async def test_remember_refuses_the_same_spellings(self, wired):
        with pytest.raises(ToolError, match="did you mean 'acme/api'"):
            await server.remember("content", tenant="ACME/API")


class TestDerivation:
    async def test_an_absent_tenant_comes_from_the_git_remote(self, wired, monkeypatch):
        _derives(monkeypatch, f"git@github.com:{DERIVED}.git")
        seen = _capture_tenant(monkeypatch)

        await server.recall("anything")

        assert seen == [DERIVED]

    async def test_configuration_still_wins_outside_a_checkout(self, wired, monkeypatch):
        _derives(monkeypatch, "")
        seen = _capture_tenant(monkeypatch)

        await server.recall("anything")

        assert seen == ["default"]

    async def test_an_explicit_tenant_overrides_a_derivable_location(self, wired, monkeypatch):
        _derives(monkeypatch, f"git@github.com:{DERIVED}.git")
        seen = _capture_tenant(monkeypatch)

        await server.recall("anything", tenant=CANONICAL)

        assert seen == [CANONICAL]


class TestAliasesAreFollowed:
    async def test_a_read_of_a_merged_tenant_reaches_its_canonical(self, wired, monkeypatch):
        """The silent-unreachability bug, asserted directly."""
        await TenantAliasRepository(wired).register(ALIAS, CANONICAL)
        seen = _capture_tenant(monkeypatch)

        await server.recall("anything", tenant=ALIAS)

        assert seen == [CANONICAL]

    async def test_an_unmerged_tenant_is_left_alone(self, wired, monkeypatch):
        seen = _capture_tenant(monkeypatch)

        await server.recall("anything", tenant=ALIAS)

        assert seen == [ALIAS]

    async def test_a_write_lands_in_the_canonical_tenant(self, wired, provider):
        """Writing under a folded spelling would re-fragment what a merge joined."""
        await TenantAliasRepository(wired).register(ALIAS, CANONICAL)

        await server.remember("content for a merged tenant", tenant=ALIAS)

        rows = await wired.execute(
            "SELECT tenant FROM memories WHERE content = %s;", ("content for a merged tenant",)
        )
        assert [r["tenant"] for r in rows] == [CANONICAL]
        await wired.execute("DELETE FROM memories WHERE content = %s;", ("content for a merged tenant",))

    async def test_a_derived_tenant_also_follows_its_alias(self, wired, monkeypatch):
        await TenantAliasRepository(wired).register(DERIVED, CANONICAL)
        _derives(monkeypatch, f"git@github.com:{DERIVED}.git")
        seen = _capture_tenant(monkeypatch)

        await server.recall("anything")

        assert seen == [CANONICAL]
