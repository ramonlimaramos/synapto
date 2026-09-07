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
with structured metadata, and **a task has exactly one**: agents find it with a
`metadata_filter` lookup, read it with `get_memory`, extend it with
`update_memory` as the state advances, and create it with `remember` only when
none exists. The packet's appended log is the task's history; a second memory
for the same `task_id` is a defect.

This is advisory coordination, not a hard lock. It works even when the sender
and receiver are never online at the same time.

## What Happens Under The Hood

| Natural request | Agent behavior |
|---|---|
| "Leave this for Claude." | Sender looks the task's packet up; extends it, or creates a `project` / `handoff` memory with `metadata.kind = "handoff"` when none exists. |
| "Continue from this handoff ID." | Receiver calls `get_memory(id)`, verifies metadata, and fetches any `context_ids`. |
| "Any handoffs for me?" | Receiver uses `handoff_inbox`, a `metadata_filter` lookup on `kind`, `to_agent` and `status`. |
| "Send it back for review." | Agent extends the same packet with `update_memory(append=…, metadata_patch={"status": …})`. |

## Lifecycle

1. **Create** — the sender summarizes goal, state, scope, decisions, validation,
   and next action in a handoff memory.
2. **Discover** — the receiver either opens a known memory ID with `get_memory`
   or searches an inbox with `recall`.
3. **Verify** — the receiver checks `metadata.kind`, `task_id`, `to_agent`,
   `status`, `files_scope`, and supporting `context_ids` before acting.
4. **Follow up** — progress is appended to the same packet with
   `update_memory(append=…)`, and the state transition is a `metadata_patch`
   on `status`, `phase`, `from_agent` and `to_agent`. The trail stays auditable
   inside the packet, dated entry by dated entry.
5. **Retire** — when the task is done, one `update_memory` call sets
   `status: "done"` and `depth_layer: "ephemeral"`, so maintenance removes the
   packet after its grace period.

## Storage Model

The MVP stores handoffs in the existing `memories` table:

| Synapto field | Value |
|---|---|
| `memory_type` | `project` |
| `subtype` | `handoff` |
| `origin` | `agent` — an agent wrote it, so automated maintenance may retire it |
| `depth_layer` | `working` while the task is open; `ephemeral` once its status is `done` |
| `scopes` | `["area:<discipline>"]` only. Scopes are conditions a reader must name, so the repository is a metadata fact, not a scope |
| `tenant` | Omitted inside the repository (derived from the git remote); `<owner>/workspace` for a task spanning repositories |
| `summary` | Routing title, for example `handoff:synapto-123 ready_for_implementation -> claude-opus-4.7` |
| `content` | Human-readable state packet with goal, decisions, scope, next action, validation, and blockers, followed by dated entries appended as the task advances |
| `metadata.kind` | `handoff` (`agent_handoff` while `schema_version` was 1; readers accept both) |
| `metadata.schema_version` | `2` |

No migration is required. The metadata is JSONB, and `recall(metadata_filter=…)`
is an exact match on it, so the inbox is a lookup rather than a ranked search.

## Metadata Schema

Required fields:

```json
{
  "kind": "handoff",
  "schema_version": 2,
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
| `done` | Work is finished and summarized; the same call moves the packet to `ephemeral` |

Teams can add their own statuses, but `ready_for_*`, `blocked`, and `done`
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
this document: look the packet up, extend it, and only otherwise create it.

Manual equivalent — the lookup first:

```text
recall("synapto-telemetry-cli", metadata_filter={"kind": "handoff", "task_id": "synapto-telemetry-cli"})
update_memory(
  memory_id="<packet id>",
  append="\n\n## 2026-09-07 — review\nImplemented the command; tests green; next: review.",
  metadata_patch={"status": "ready_for_review", "phase": "review", "from_agent": "claude-opus-4.7", "to_agent": "codex-gpt-5.5"},
)
```

Only when the lookup returns nothing:

```text
remember(
  content="Goal: add telemetry CLI commands. Current state: ... Decisions: ... Next action: ...",
  memory_type="project",
  subtype="handoff",
  depth_layer="working",
  origin="agent",
  scopes=["area:software-engineering"],
  summary="handoff:synapto-telemetry-cli ready_for_implementation -> claude-opus-4.7",
  metadata={
    "kind": "handoff",
    "schema_version": 2,
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
  "handoff",
  tenant="synapto",
  metadata_filter={"kind": "handoff", "to_agent": "claude-opus-4.7", "status": "ready_for_implementation"},
  limit=10,
  preview_chars=200
)
get_memory("<handoff-id>")
```

`metadata_filter` is an exact match, so the result is the packet rather than a
ranked candidate list. Repeat the call with `"kind": "agent_handoff"` for packets
whose `schema_version` is 1, and read the full packet with `get_memory` before acting.

If the handoff includes `context_ids`, fetch those too:

```text
get_memories(["<context-id-1>", "<context-id-2>"])
```

## Ownership And Safety

Handoffs are not locks. Agents should treat `files_scope` as an advisory claim:

- Work only inside `files_scope` unless the user expands the scope.
- If two active handoffs appear to own the same files, stop and ask the user.
- Extend the packet instead of writing a sibling: one memory per `task_id`.
- Use the same `task_id` for all status updates so the lookup returns the thread.
- Keep sensitive secrets out of `content` and `metadata`; Synapto persists both.

## Example Workflow

1. Codex plans a change and creates a handoff for Claude.
2. Claude searches its inbox with `handoff_inbox` or `recall`.
3. Claude fetches the full handoff with `get_memory`.
4. Claude implements the scoped files and extends the packet with
   `status=ready_for_review`.
5. Codex looks the packet up, reviews the work, and extends it again with
   `status=done` and `depth_layer=ephemeral`, or `status=blocked`.
