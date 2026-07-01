"""Unit tests for Synapto MCP tool metadata — `alwaysLoad` flag (issue #15)."""

from __future__ import annotations

import pytest

from synapto.server import ALWAYS_LOAD_META, mcp, ping

ALWAYS_LOAD_TOOLS = ("remember", "recall")
DEFERRED_TOOLS = (
    "relate",
    "forget",
    "trust_feedback",
    "find_contradictions",
    "get_memory",
    "get_memories",
    "update_memory",
    "agent_handoff_template",
    "handoff_inbox_template",
    "ping",
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
    assert tool.meta == ALWAYS_LOAD_META, f"{name!r} must expose alwaysLoad metadata so Claude Code loads it eagerly"


@pytest.mark.parametrize("name", DEFERRED_TOOLS)
async def test_non_critical_tools_stay_deferred(name: str):
    """Only `remember` and `recall` should be marked alwaysLoad — the rest must stay deferred
    so they do not bloat the LLM's tool list on every session."""
    tool = await mcp.get_tool(name)
    assert tool.meta is None, f"{name!r} should not carry alwaysLoad metadata"


async def test_memory_tool_descriptions_document_hard_limits():
    remember = await mcp.get_tool("remember")
    update_memory = await mcp.get_tool("update_memory")
    get_memories = await mcp.get_tool("get_memories")

    assert "summary: optional short summary (max 255 chars)" in remember.description
    assert "memory_type" in remember.description and "max 20 chars" in remember.description
    assert "subtype" in remember.description and "max 50 chars" in remember.description
    assert "tenant" in remember.description and "max 100 chars" in remember.description
    assert "depth_layer" in remember.description and "max 20 chars" in remember.description
    assert "summary: optional replacement summary (max 255 chars)" in update_memory.description
    assert "memory_ids: UUIDs of memories to fetch (max 20)" in get_memories.description


async def test_remember_description_guides_automatic_memory_capture():
    remember = await mcp.get_tool("remember")

    for phrase in (
        "durable preference",
        "workflow rule",
        "project context",
        "Store the memory in Synapto instead of writing flat files",
        "Recommended memory_type choices",
        "Recommended subtypes",
        "Recommended depth_layer choices",
        "code_style",
        "workflow",
        "external_system",
        "feedback",
        "project",
        "reference",
        "user",
        "core",
        "stable",
        "working",
        "ephemeral",
    ):
        assert phrase in remember.description


async def test_recall_description_guides_proactive_context_loading():
    recall = await mcp.get_tool("recall")

    for phrase in (
        "at the start of any non-trivial task",
        "prior decisions",
        "preferences",
        "Recall proactively",
        "Use tenant to scope project-specific memory",
        "subtype to narrow within a memory_type",
        "Follow up with get_memory",
    ):
        assert phrase in recall.description


async def test_ping_tool_is_lightweight_health_check():
    ping = await mcp.get_tool("ping")

    assert ping.meta is None
    assert "transport health" in ping.description
    assert "does not touch databases, cache, or embeddings" in ping.description


async def test_ping_returns_pong_without_dependencies():
    assert await ping() == "pong"
