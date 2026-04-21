"""Unit tests for Synapto MCP tool metadata — `alwaysLoad` flag (issue #15)."""

from __future__ import annotations

import pytest

from synapto.server import ALWAYS_LOAD_META, mcp

ALWAYS_LOAD_TOOLS = ("remember", "recall")
DEFERRED_TOOLS = (
    "relate",
    "forget",
    "trust_feedback",
    "find_contradictions",
    "graph_query",
    "list_entities_tool",
    "memory_stats",
    "maintain",
)


def test_always_load_meta_constant_shape():
    assert ALWAYS_LOAD_META == {"alwaysLoad": True}


@pytest.mark.parametrize("name", ALWAYS_LOAD_TOOLS)
async def test_critical_tools_carry_always_load_meta(name: str):
    """`remember` and `recall` must ship `alwaysLoad: true` so MCP clients skip ToolSearch."""
    tool = await mcp.get_tool(name)
    assert tool.meta == ALWAYS_LOAD_META, (
        f"{name!r} must expose alwaysLoad metadata so Claude Code loads it eagerly"
    )


@pytest.mark.parametrize("name", DEFERRED_TOOLS)
async def test_non_critical_tools_stay_deferred(name: str):
    """Only `remember` and `recall` should be marked alwaysLoad — the rest must stay deferred
    so they do not bloat the LLM's tool list on every session."""
    tool = await mcp.get_tool(name)
    assert tool.meta is None, f"{name!r} should not carry alwaysLoad metadata"
