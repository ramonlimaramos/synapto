"""Unit tests for Synapto MCP server instructions (issue #11)."""

from __future__ import annotations

from synapto.server import SERVER_INSTRUCTIONS, mcp


def test_server_instructions_constant_is_non_empty():
    assert isinstance(SERVER_INSTRUCTIONS, str)
    assert SERVER_INSTRUCTIONS.strip(), "SERVER_INSTRUCTIONS must not be empty"


def test_server_instructions_mention_core_tools():
    """Instructions must reference the main tools so the LLM knows when to use them."""
    for tool in ("recall", "remember"):
        assert tool in SERVER_INSTRUCTIONS, f"instructions should mention {tool!r}"


def test_server_instructions_cover_depth_layers():
    for layer in ("core", "stable", "working", "ephemeral"):
        assert layer in SERVER_INSTRUCTIONS, f"instructions should mention {layer!r}"


def test_server_instructions_cover_memory_types():
    for mtype in ("feedback", "project", "user", "reference"):
        assert mtype in SERVER_INSTRUCTIONS, f"instructions should mention {mtype!r}"


def test_server_instructions_cover_handoffs():
    for term in ("agent_handoff", "task_id", "get_memory", "files_scope"):
        assert term in SERVER_INSTRUCTIONS, f"instructions should mention {term!r}"


def test_fastmcp_instance_has_instructions_attached():
    """The FastMCP instance must expose the instructions so MCP clients can inject them."""
    assert mcp.instructions == SERVER_INSTRUCTIONS
