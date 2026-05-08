# Cross-Agent Handoffs

Synapto can coordinate work between LLM agents, IDE assistants, and coding
sessions without adding a separate task database. A handoff is a normal Synapto
memory with structured metadata. Agents discover it with `recall`, fetch the
complete packet with `get_memory`, and append follow-up memories with `remember`.

This is advisory coordination, not a hard lock. It works even when the sender
and receiver are never online at the same time.

## Storage Model

The MVP stores handoffs in the existing `memories` table:

| Synapto field | Value |
|---|---|
| `memory_type` | `project` |
| `depth_layer` | `working` for active work, `ephemeral` for short-lived session transfer |
| `summary` | Routing title, for example `handoff:synapto-123 ready_for_implementation -> claude-opus-4.7` |
| `content` | Human-readable state packet with goal, decisions, scope, next action, validation, and blockers |
| `metadata.kind` | `agent_handoff` |
| `metadata.schema_version` | `1` |

No migration is required. The metadata is JSONB and can evolve without changing
the schema. Later versions can add deterministic metadata queries or claim tools
if advisory handoffs are not enough.

## Metadata Schema

Required fields:

```json
{
  "kind": "agent_handoff",
  "schema_version": 1,
  "task_id": "synapto-telemetry-cli",
  "from_agent": "codex-gpt-5.5",
  "to_agent": "claude-opus-4.7",
  "phase": "planning",
  "status": "ready_for_implementation",
  "repo": "/Users/ramonramos/Developer/personal/python/synapto",
  "branch": "feat/telemetry-cli",
  "files_scope": ["src/synapto/cli.py", "tests/unit/test_cli.py"],
  "context_ids": ["550e8400-e29b-41d4-a716-446655440000"],
  "next_action": "Implement the CLI command and tests"
}
```

Common statuses:

| Status | Meaning |
|---|---|
| `ready_for_implementation` | Another agent should implement the plan |
| `ready_for_review` | Another agent should review or validate the work |
| `blocked` | Work cannot continue without user or system input |
| `completed` | Work is done and summarized |

## Creating A Handoff

Clients that support MCP prompts can use the Synapto prompt:

```text
/mcp__synapto__agent_handoff \
  synapto-telemetry-cli \
  codex-gpt-5.5 \
  claude-opus-4.7 \
  planning \
  ready_for_implementation
```

Claude Code exposes MCP prompts as slash commands in the form
`/mcp__servername__promptname`. Other clients may expose prompts differently;
if a client does not support MCP prompts, ask the agent to follow this document
and call `remember` directly.

Manual equivalent:

```text
remember(
  content="Goal: add telemetry CLI commands. Current state: ... Decisions: ... Next action: ...",
  memory_type="project",
  depth_layer="working",
  summary="handoff:synapto-telemetry-cli ready_for_implementation -> claude-opus-4.7",
  metadata={
    "kind": "agent_handoff",
    "schema_version": 1,
    "task_id": "synapto-telemetry-cli",
    "from_agent": "codex-gpt-5.5",
    "to_agent": "claude-opus-4.7",
    "phase": "planning",
    "status": "ready_for_implementation",
    "repo": "/Users/ramonramos/Developer/personal/python/synapto",
    "branch": "feat/telemetry-cli",
    "files_scope": ["src/synapto/cli.py", "tests/unit/test_cli.py"],
    "context_ids": [],
    "next_action": "Implement the CLI command and tests"
  }
)
```

## Receiving A Handoff

Clients that support MCP prompts can use:

```text
/mcp__synapto__handoff_inbox claude-opus-4.7 synapto
```

Manual equivalent:

```text
recall(
  "kind:agent_handoff to_agent:claude-opus-4.7 status:ready_for_implementation",
  tenant="synapto",
  depth_layer="working",
  limit=10,
  preview_chars=200
)
get_memory("<handoff-id>")
```

If the handoff includes `context_ids`, fetch those too:

```text
get_memories(["<context-id-1>", "<context-id-2>"])
```

## Ownership And Safety

Handoffs are not locks. Agents should treat `files_scope` as an advisory claim:

- Work only inside `files_scope` unless the user expands the scope.
- If two active handoffs appear to own the same files, stop and ask the user.
- Append follow-up memories instead of mutating old handoffs.
- Use the same `task_id` for all status updates so `recall` can reconstruct the thread.
- Keep sensitive secrets out of `content` and `metadata`; Synapto persists both.

## Example Workflow

1. Codex plans a change and creates a handoff for Claude.
2. Claude searches its inbox with `handoff_inbox` or `recall`.
3. Claude fetches the full handoff with `get_memory`.
4. Claude implements the scoped files and writes a follow-up handoff with
   `status=ready_for_review`.
5. Codex recalls handoffs for itself, fetches the update, reviews the work, and
   appends `status=completed` or `status=blocked`.

