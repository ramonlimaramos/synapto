"""Tests for typed scopes at the MCP boundary.

The scope machinery was already built and tested at four layers — schema,
value objects, repository, search — and reachable from none of them, because
`server.py` exposed only `domain`. So these tests are about the adapter: that
the compact string form arrives intact, that the two axes cannot be combined,
that a rejection surfaces as a tool error rather than a traceback, and that
`domain`-only calls behave exactly as they did.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError

from synapto import server
from synapto.repositories.memories import MemoryRepository
from synapto.scopes import ScopeRef, ScopeSet

TENANT = "acme/scope-tools"


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
        "DELETE FROM memory_scopes WHERE memory_id IN (SELECT id FROM memories WHERE tenant = %s);", (TENANT,)
    )
    await pg.execute(
        "DELETE FROM memory_entities WHERE memory_id IN (SELECT id FROM memories WHERE tenant = %s);", (TENANT,)
    )
    await pg.execute("DELETE FROM memories WHERE tenant = %s;", (TENANT,))


async def _stored_scopes(pg, content: str) -> ScopeSet:
    rows = await pg.execute("SELECT id FROM memories WHERE tenant = %s AND content = %s;", (TENANT, content))
    row = await MemoryRepository(pg).get_by_id(rows[0]["id"])
    return row["scopes"]


def _capture_scopes(monkeypatch) -> list:
    seen = []

    async def fake_search(*_args, scopes=None, **_kwargs):
        seen.append(scopes)
        return []

    monkeypatch.setattr(server, "hybrid_search", fake_search)
    return seen


class TestRememberPersistsScopes:
    async def test_compact_strings_become_memberships(self, wired):
        await server.remember("scoped content", tenant=TENANT, scopes=["language:python", "repo:acme/api"])

        stored = await _stored_scopes(wired, "scoped content")

        assert stored == ScopeSet.parse([ScopeRef("language", "python"), ScopeRef("repo", "acme/api")])

    async def test_a_memory_without_scopes_stays_unscoped(self, wired):
        await server.remember("plain content", tenant=TENANT)

        assert await _stored_scopes(wired, "plain content") == ScopeSet()

    async def test_an_empty_list_is_not_an_error(self, wired):
        await server.remember("explicitly unscoped", tenant=TENANT, scopes=[])

        assert await _stored_scopes(wired, "explicitly unscoped") == ScopeSet()

    async def test_a_rejected_scope_stores_nothing(self, wired):
        """The parse happens before the embedding, so a bad scope costs no work."""
        with pytest.raises(ToolError):
            await server.remember("never stored", tenant=TENANT, scopes=["language:Python"])

        rows = await wired.execute("SELECT id FROM memories WHERE content = %s;", ("never stored",))
        assert rows == []


class TestRecallFiltersByScope:
    async def test_compact_strings_reach_the_search_as_a_scope_set(self, wired, monkeypatch):
        seen = _capture_scopes(monkeypatch)

        await server.recall("anything", tenant=TENANT, scopes=["language:python", "repo:acme/api"])

        assert seen == [ScopeSet.parse([ScopeRef("language", "python"), ScopeRef("repo", "acme/api")])]

    async def test_no_scopes_argument_leaves_the_filter_absent(self, wired, monkeypatch):
        seen = _capture_scopes(monkeypatch)

        await server.recall("anything", tenant=TENANT)

        assert seen == [None]

    async def test_an_empty_list_is_an_assertion_not_an_absence(self, wired, monkeypatch):
        seen = _capture_scopes(monkeypatch)

        await server.recall("anything", tenant=TENANT, scopes=[])

        assert seen == [ScopeSet()]

    async def test_scoped_recall_finds_only_matching_memories(self, wired):
        """End to end, through the real search rather than a stub."""
        await server.remember("python guidance", tenant=TENANT, scopes=["language:python"])
        await server.remember("elixir guidance", tenant=TENANT, scopes=["language:elixir"])

        found = await server.recall("guidance", tenant=TENANT, scopes=["language:python"])

        assert "python guidance" in found
        assert "elixir guidance" not in found

    async def test_a_query_type_the_memory_does_not_carry_imposes_nothing(self, wired):
        """The Option B rule: AND across the types the *memory* carries, not the query."""
        await server.remember("both axes", tenant=TENANT, scopes=["language:python", "repo:acme/api"])
        await server.remember("one axis", tenant=TENANT, scopes=["language:python"])

        found = await server.recall("axes", tenant=TENANT, scopes=["language:python", "repo:acme/api"])

        assert "both axes" in found
        assert "one axis" in found

    async def test_a_conflicting_key_on_a_type_the_memory_carries_excludes_it(self, wired):
        await server.remember("wrong repo", tenant=TENANT, scopes=["language:python", "repo:acme/other"])

        found = await server.recall("wrong", tenant=TENANT, scopes=["language:python", "repo:acme/api"])

        assert "wrong repo" not in found

    async def test_an_unscoped_memory_is_excluded_when_a_filter_is_given(self, wired):
        await server.remember("unscoped entry", tenant=TENANT)

        found = await server.recall("unscoped", tenant=TENANT, scopes=["language:python"])

        assert "unscoped entry" not in found

    async def test_a_global_memory_always_matches(self, wired):
        await server.remember("applies everywhere", tenant=TENANT, scopes=["global:all"])

        found = await server.recall("applies", tenant=TENANT, scopes=["language:python"])

        assert "applies everywhere" in found


class TestTheTwoAxesCannotBeCombined:
    async def test_domain_and_scopes_together_are_refused(self, wired):
        with pytest.raises(ToolError, match="domain and scopes cannot be combined"):
            await server.remember("conflicted", tenant=TENANT, domain="python", scopes=["language:python"])

    async def test_recall_refuses_the_same_combination(self, wired):
        with pytest.raises(ToolError, match="domain and scopes cannot be combined"):
            await server.recall("anything", tenant=TENANT, domain="python", scopes=["language:python"])

    async def test_an_empty_scope_list_still_counts_as_supplied(self, wired):
        """`[]` asserts something about scopes; it is not an absence."""
        with pytest.raises(ToolError, match="domain and scopes cannot be combined"):
            await server.recall("anything", tenant=TENANT, domain="python", scopes=[])


class TestRejectionsSurfaceAsToolErrors:
    @pytest.mark.parametrize(
        ("bad", "expected"),
        [
            (["language:Python"], "did you mean 'python'"),
            (["python"], "missing its type"),
            (["dialect:python"], "unknown scope type"),
            (["repo:https://github.com/acme/api"], "canonical 'owner/repo'"),
            (["global:all", "language:python"], "cannot be combined with other scopes"),
            ([7], "each scope must be"),
            ("language:python", "must be a list"),
        ],
    )
    async def test_a_bad_scope_is_a_tool_error_not_a_traceback(self, wired, bad, expected):
        with pytest.raises(ToolError, match=expected):
            await server.recall("anything", tenant=TENANT, scopes=bad)


class TestUpdateMemoryRescopes:
    async def _stored_id(self, pg, content):
        rows = await pg.execute("SELECT id FROM memories WHERE tenant = %s AND content = %s;", (TENANT, content))
        return str(rows[0]["id"])

    async def test_scopes_can_be_replaced(self, wired):
        await server.remember("rescope me", tenant=TENANT, scopes=["language:python"])
        memory_id = await self._stored_id(wired, "rescope me")

        await server.update_memory(memory_id, scopes=["language:elixir"])

        assert await _stored_scopes(wired, "rescope me") == ScopeSet.parse([ScopeRef("language", "elixir")])

    async def test_an_empty_list_clears_them(self, wired):
        await server.remember("clear me", tenant=TENANT, scopes=["language:python"])
        memory_id = await self._stored_id(wired, "clear me")

        await server.update_memory(memory_id, scopes=[])

        assert await _stored_scopes(wired, "clear me") == ScopeSet()

    async def test_omitting_scopes_preserves_them(self, wired):
        await server.remember("keep my scopes", tenant=TENANT, scopes=["language:python"])
        memory_id = await self._stored_id(wired, "keep my scopes")

        await server.update_memory(memory_id, summary="a new summary")

        assert await _stored_scopes(wired, "keep my scopes") == ScopeSet.parse([ScopeRef("language", "python")])

    async def test_rescoping_alone_is_a_valid_update(self, wired):
        await server.remember("scopes only", tenant=TENANT)
        memory_id = await self._stored_id(wired, "scopes only")

        result = await server.update_memory(memory_id, scopes=["language:python"])

        assert "scopes" in result

    async def test_a_rejected_rescope_changes_nothing(self, wired):
        await server.remember("unchanged", tenant=TENANT, scopes=["language:python"])
        memory_id = await self._stored_id(wired, "unchanged")

        with pytest.raises(ToolError):
            await server.update_memory(memory_id, scopes=["language:Elixir"])

        assert await _stored_scopes(wired, "unchanged") == ScopeSet.parse([ScopeRef("language", "python")])


class TestDomainOnlyCallsAreUnaffected:
    """The regression that matters most: every memory written so far uses domain."""

    async def test_a_domain_write_still_persists_the_domain(self, wired):
        await server.remember("domain content", tenant=TENANT, domain="python")

        rows = await wired.execute("SELECT domain FROM memories WHERE content = %s;", ("domain content",))

        assert rows[0]["domain"] == "python"

    async def test_a_domain_write_creates_no_scopes(self, wired):
        await server.remember("domain content", tenant=TENANT, domain="python")

        assert await _stored_scopes(wired, "domain content") == ScopeSet()

    async def test_domain_recall_still_filters(self, wired):
        await server.remember("python by domain", tenant=TENANT, domain="python")
        await server.remember("elixir by domain", tenant=TENANT, domain="elixir")

        found = await server.recall("by domain", tenant=TENANT, domain="python")

        assert "python by domain" in found
        assert "elixir by domain" not in found


class TestScopesAreVisibleInOutput:
    async def test_get_memory_renders_them_in_the_form_it_accepts(self, wired):
        await server.remember("visible scopes", tenant=TENANT, scopes=["language:python", "repo:acme/api"])
        rows = await wired.execute("SELECT id FROM memories WHERE content = %s;", ("visible scopes",))

        rendered = await server.get_memory(str(rows[0]["id"]))

        assert "scopes: language:python, repo:acme/api" in rendered

    async def test_an_unscoped_memory_renders_no_scope_line(self, wired):
        await server.remember("no scopes here", tenant=TENANT)
        rows = await wired.execute("SELECT id FROM memories WHERE content = %s;", ("no scopes here",))

        rendered = await server.get_memory(str(rows[0]["id"]))

        assert "scopes:" not in rendered

    async def test_recall_shows_the_scopes_of_each_hit(self, wired):
        await server.remember("recallable scoped", tenant=TENANT, scopes=["language:python"])

        found = await server.recall("recallable", tenant=TENANT)

        assert "scopes=language:python" in found
