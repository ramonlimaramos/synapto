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
    assert '"kind": "agent_handoff"' in text
    assert '"files_scope": [' in text
    assert "Call `remember` exactly once" in text


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

    assert "Call `remember` exactly once" in text
    assert '"kind": "agent_handoff"' in text
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
    assert "agent handoff for codex-gpt-5.5" in text
    assert "status ready_for_review" in text
    assert "task synapto-123" in text
    assert "preview_chars=200" in text
    assert "get_memory(id)" in text


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
    assert "agent handoff for codex-gpt-5.5" in text
    assert "status ready_for_review" in text
    assert "task synapto-123" in text
    assert "preview_chars=200" in text
    assert "get_memory(id)" in text
