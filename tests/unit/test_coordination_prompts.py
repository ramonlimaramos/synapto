"""Tests for Synapto MCP prompts that expose cross-agent coordination commands."""

from __future__ import annotations

from synapto.server import mcp


def _prompt_text(result) -> str:
    return "\n".join(message.content.text for message in result.messages)


def _tool_text(result) -> str:
    return "\n".join(content.text for content in result.content)


async def test_coordination_prompts_are_registered() -> None:
    prompts = await mcp.list_prompts()
    names = {prompt.name for prompt in prompts}

    assert "agent_handoff" in names
    assert "handoff_inbox" in names


async def test_coordination_template_tools_are_registered() -> None:
    tools = await mcp.list_tools()
    names = {tool.name for tool in tools}

    assert "agent_handoff_template" in names
    assert "handoff_inbox_template" in names


async def test_agent_handoff_prompt_renders_task_specific_contract() -> None:
    result = await mcp.render_prompt(
        "agent_handoff",
        {
            "task_id": "synapto-123",
            "from_agent": "codex-gpt-5.5",
            "to_agent": "claude-opus-4.7",
            "phase": "planning",
            "status": "ready_for_implementation",
            "repo": "/repo/synapto",
            "branch": "feat/handoff",
            "files_scope": "src/synapto/server.py, docs/agent-handoffs.md",
            "context_ids": "11111111-1111-1111-1111-111111111111",
            "next_action": "Implement the plan",
            "summary": "Handoff for implementation",
        },
    )

    text = _prompt_text(result)
    assert "task_id: `synapto-123`" in text
    assert "from_agent: `codex-gpt-5.5`" in text
    assert "to_agent: `claude-opus-4.7`" in text
    assert '"kind": "handoff"' in text
    assert '"files_scope": [' in text
    assert "call `remember` exactly once" in text


async def test_agent_handoff_prompt_looks_the_packet_up_before_writing() -> None:
    """One packet per task: the lookup and the update come before the create path."""
    result = await mcp.render_prompt(
        "agent_handoff",
        {"task_id": "synapto-123", "from_agent": "codex", "to_agent": "claude", "status": "ready_for_review"},
    )

    text = _prompt_text(result)
    assert 'metadata_filter={"kind": "handoff", "task_id": "synapto-123"}' in text
    assert 'metadata_filter={"kind": "agent_handoff", "task_id": "synapto-123"}' in text
    assert "update_memory(" in text
    assert "never write a sibling" in text
    assert text.index("update_memory(") < text.index("call `remember` exactly once")
    assert 'append="\\n\\n## <date> — planning\\n' in text


async def test_agent_handoff_prompt_prescribes_the_writing_contract() -> None:
    result = await mcp.render_prompt(
        "agent_handoff",
        {"task_id": "synapto-123", "from_agent": "codex", "to_agent": "claude", "repo": "acme/api"},
    )

    text = _prompt_text(result)
    assert "subtype: `handoff`" in text
    assert "origin: `agent`" in text
    assert 'scopes: `["area:software-engineering"]`' in text
    assert "the repository stays in `metadata.repo`" in text
    assert '"repo": "acme/api"' in text
    assert "tenant: omit it inside the repository" in text
    assert "`depth_layer` to `ephemeral`" in text


async def test_agent_handoff_prompt_area_is_a_parameter() -> None:
    result = await mcp.render_prompt(
        "agent_handoff",
        {"task_id": "budget-2026", "from_agent": "codex", "to_agent": "claude", "area": "finance"},
    )

    assert 'scopes: `["area:finance"]`' in _prompt_text(result)


async def test_agent_handoff_template_tool_renders_same_contract() -> None:
    tool = await mcp.get_tool("agent_handoff_template")
    result = await tool.run(
        {
            "task_id": "synapto-123",
            "from_agent": "codex-gpt-5.5",
            "to_agent": "claude-opus-4.7",
            "phase": "planning",
            "status": "ready_for_implementation",
            "repo": "/repo/synapto",
            "branch": "feat/handoff",
            "files_scope": "src/synapto/server.py, docs/agent-handoffs.md",
            "context_ids": "11111111-1111-1111-1111-111111111111",
            "next_action": "Implement the plan",
            "summary": "Handoff for implementation",
        }
    )
    text = _tool_text(result)

    assert "call `remember` exactly once" in text
    assert '"kind": "handoff"' in text
    assert "update_memory(" in text
    assert "to_agent: `claude-opus-4.7`" in text


async def test_handoff_inbox_prompt_renders_recall_instruction() -> None:
    result = await mcp.render_prompt(
        "handoff_inbox",
        {
            "agent": "codex-gpt-5.5",
            "tenant": "synapto",
            "task_id": "synapto-123",
            "status": "ready_for_review",
            "limit": "5",
        },
    )

    text = _prompt_text(result)
    assert "tenant=`synapto`" in text
    assert (
        'metadata_filter=`{"kind": "handoff", "to_agent": "codex-gpt-5.5", '
        '"status": "ready_for_review", "task_id": "synapto-123"}`'
    ) in text
    assert '{"kind": "agent_handoff", "to_agent": "codex-gpt-5.5"' in text
    assert "preview_chars=200" in text
    assert "get_memory(id)" in text
    assert "extend the SAME packet" in text
    assert 'depth_layer="ephemeral"' in text


async def test_handoff_inbox_prompt_without_tenant_reads_repository_then_workspace() -> None:
    result = await mcp.render_prompt("handoff_inbox", {"agent": "claude"})

    text = _prompt_text(result)
    assert "tenant omitted (the repository's), then tenant=`<owner>/workspace`" in text
    assert '{"kind": "handoff", "to_agent": "claude", "status": "ready_for_implementation"}' in text


async def test_handoff_inbox_template_tool_renders_recall_instruction() -> None:
    tool = await mcp.get_tool("handoff_inbox_template")
    result = await tool.run(
        {
            "agent": "codex-gpt-5.5",
            "tenant": "synapto",
            "task_id": "synapto-123",
            "status": "ready_for_review",
            "limit": "5",
        }
    )
    text = _tool_text(result)

    assert "tenant=`synapto`" in text
    assert '"to_agent": "codex-gpt-5.5"' in text
    assert '"status": "ready_for_review"' in text
    assert '"task_id": "synapto-123"' in text
    assert "preview_chars=200" in text
    assert "get_memory(id)" in text
