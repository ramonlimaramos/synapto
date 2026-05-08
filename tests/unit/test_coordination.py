"""Tests for the cross-agent handoff prompt helpers."""

from __future__ import annotations

import json

from synapto.coordination import (
    HANDOFF_KIND,
    HANDOFF_SCHEMA_VERSION,
    build_handoff_metadata,
    render_agent_handoff_prompt,
    render_handoff_inbox_prompt,
)


def test_build_handoff_metadata_normalizes_list_fields() -> None:
    metadata = build_handoff_metadata(
        task_id="synapto-123",
        from_agent="codex-gpt-5.5",
        to_agent="claude-opus-4.7",
        phase="planning",
        status="ready_for_implementation",
        repo="/repo/synapto",
        branch="feat/handoff",
        files_scope="src/synapto/server.py, docs/agent-handoffs.md",
        context_ids="11111111-1111-1111-1111-111111111111, 22222222-2222-2222-2222-222222222222",
        next_action="Implement the plan",
        pr_url="https://github.com/example/repo/pull/1",
    )

    assert metadata == {
        "kind": HANDOFF_KIND,
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "task_id": "synapto-123",
        "from_agent": "codex-gpt-5.5",
        "to_agent": "claude-opus-4.7",
        "phase": "planning",
        "status": "ready_for_implementation",
        "repo": "/repo/synapto",
        "branch": "feat/handoff",
        "files_scope": ["src/synapto/server.py", "docs/agent-handoffs.md"],
        "context_ids": [
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        ],
        "next_action": "Implement the plan",
        "pr_url": "https://github.com/example/repo/pull/1",
    }


def test_render_agent_handoff_prompt_contains_remember_contract() -> None:
    prompt = render_agent_handoff_prompt(
        task_id="synapto-123",
        from_agent="codex-gpt-5.5",
        to_agent="claude-opus-4.7",
        phase="planning",
        status="ready_for_implementation",
        repo="/repo/synapto",
        branch="feat/handoff",
        files_scope="src/synapto/server.py",
        context_ids="11111111-1111-1111-1111-111111111111",
        next_action="Implement the plan",
        summary="Handoff for implementation",
    )

    assert "Call `remember` exactly once" in prompt
    assert "memory_type: `project`" in prompt
    assert "depth_layer: `working`" in prompt
    assert "handoff:synapto-123 ready_for_implementation -> claude-opus-4.7" in prompt
    assert "Do not edit files outside `files_scope`" in prompt
    assert "get_memory" in prompt

    metadata_start = prompt.index("```json\n") + len("```json\n")
    metadata_end = prompt.index("\n```", metadata_start)
    metadata = json.loads(prompt[metadata_start:metadata_end])
    assert metadata["kind"] == HANDOFF_KIND
    assert metadata["files_scope"] == ["src/synapto/server.py"]


def test_render_handoff_inbox_prompt_uses_two_stage_retrieval() -> None:
    prompt = render_handoff_inbox_prompt(
        agent="claude-opus-4.7",
        tenant="synapto",
        task_id="synapto-123",
        status="ready_for_implementation",
        limit=7,
    )

    assert "recall" in prompt
    assert "preview_chars=200" in prompt
    assert "limit=7" in prompt
    assert "kind:agent_handoff" in prompt
    assert "to_agent:claude-opus-4.7" in prompt
    assert "task_id:synapto-123" in prompt
    assert "get_memory(id)" in prompt

