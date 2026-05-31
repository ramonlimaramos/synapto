# Cross-Agent Handoffs

You speak normally. Synapto handles the structured handoff under the hood.

```text
You: Codex, plan this and leave a handoff for Claude to implement.
Codex: Handoff created for Claude: b0e1506e-d1b7-4bee-9223-4d0f8d18a1b2

You: Claude, continue from Synapto handoff b0e1506e-d1b7-4bee-9223-4d0f8d18a1b2.
Claude: I read the handoff, fetched the related context, and can continue.
```

Synapto coordinates work between LLM agents, IDE assistants, and coding sessions
without adding a separate task database. A handoff is a normal Synapto memory
with structured metadata. Agents discover candidates with `recall`, fetch the
complete packet with `get_memory`, verify the metadata, and append follow-up
memories with `remember`.

This is advisory coordination, not a hard lock. It works even when the sender
and receiver are never online at the same time.

## What Happens Under The Hood

| Natural request | Agent behavior |
|---|---|
| "Leave this for Claude." | Sender creates a `project` / `working` memory with `metadata.kind = "agent_handoff"`. |
| "Continue from this handoff ID." | Receiver calls `get_memory(id)`, verifies metadata, and fetches any `context_ids`. |
| "Any handoffs for me?" | Receiver uses `handoff_inbox` or `recall` to find ranked candidates, then verifies them. |
| "Send it back for review." | Agent appends a new handoff memory with the same `task_id` and a new `status`. |

## Lifecycle

1. **Create** — the sender summarizes goal, state, scope, decisions, validation,
   and next action in a handoff memory.
2. **Discover** — the receiver either opens a known memory ID with `get_memory`
   or searches an inbox with `recall`.
3. **Verify** — the receiver checks `metadata.kind`, `task_id`, `to_agent`,
   `status`, `files_scope`, and supporting `context_ids` before acting.
4. **Follow up** — progress is appended as a new memory with the same `task_id`;
   older handoffs are not mutated.

Use `update_memory` for small corrections or appending clarifying text to a
single memory. For state transitions between agents, prefer a new follow-up
memory so the coordination trail stays auditable.

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
the schema. Current inbox discovery uses `recall`, which is ranked full-text and
semantic search. It is not a deterministic metadata filter. Later versions can
add JSONB metadata queries or claim tools if advisory handoffs are not enough.

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
| `ready_for_validation` | Another agent should validate behavior, docs, or release readiness |
| `blocked` | Work cannot continue without user or system input |
| `completed` | Work is done and summarized |

Teams can add their own statuses, but `ready_for_*`, `blocked`, and `completed`
are the recommended shape because agents can infer intent from them.

## Creating A Handoff

Most users should ask in natural language:

```text
Codex, plan this feature and leave a handoff for Claude to implement.
```

The agent should infer the fields, create the handoff, and return only the
memory ID. The tools below are the explicit equivalents for clients or agents
that need a structured entry point.

Clients that support MCP prompts can use the Synapto prompt:

```text
/mcp__synapto__agent_handoff \
  synapto-telemetry-cli \
  codex-gpt-5.5 \
  claude-opus-4.7 \
  planning \
  ready_for_implementation
```

Clients that expose tools but not MCP prompts can call the equivalent template
tool and then follow the rendered instructions:

```text
mcp__synapto__agent_handoff_template(
  task_id="synapto-telemetry-cli",
  from_agent="codex-gpt-5.5",
  to_agent="claude-opus-4.7",
  phase="planning",
  status="ready_for_implementation"
)
```

If a client supports neither prompts nor template tools, ask the agent to follow
this document and call `remember` directly.

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

The simplest receiving flow is a memory ID:

```text
Claude, continue from Synapto handoff b0e1506e-d1b7-4bee-9223-4d0f8d18a1b2.
```

The receiving agent should call `get_memory(id)`, verify the handoff metadata,
fetch any `context_ids`, and then continue or propose a plan. If the user does
not provide an ID, use the inbox flow below.

Clients that support MCP prompts can use:

```text
/mcp__synapto__handoff_inbox claude-opus-4.7 synapto
```

Clients that expose tools but not MCP prompts can call:

```text
mcp__synapto__handoff_inbox_template(
  agent="claude-opus-4.7",
  tenant="synapto"
)
```

Manual equivalent:

```text
recall(
  "agent_handoff agent handoff for claude-opus-4.7 status ready_for_implementation",
  tenant="synapto",
  depth_layer="working",
  limit=10,
  preview_chars=200
)
get_memory("<handoff-id>")
```

The `recall` call returns ranked candidates, not rows filtered by
`metadata.to_agent` or `metadata.status`. Always inspect the full memory with
`get_memory`, then verify `metadata.kind`, `metadata.to_agent`,
`metadata.status`, and `metadata.task_id` before acting.

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
