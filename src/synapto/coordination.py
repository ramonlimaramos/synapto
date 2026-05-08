"""Cross-agent coordination helpers.

The MVP deliberately stores handoffs as ordinary project memories. This keeps
coordination portable across MCP clients: agents use ``remember`` to append a
handoff, ``recall`` to discover candidates, and ``get_memory`` / ``get_memories``
to fetch complete context.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

HANDOFF_KIND = "agent_handoff"
HANDOFF_SCHEMA_VERSION = 1
DEFAULT_HANDOFF_LIMIT = 10


def _split_csv(value: str | Iterable[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = value.split(",")
    else:
        items = value
    return [item.strip() for item in items if item and item.strip()]


def _coerce_limit(value: int | str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_HANDOFF_LIMIT
    return max(1, min(parsed, 50))


def build_handoff_metadata(
    *,
    task_id: str,
    from_agent: str,
    to_agent: str,
    phase: str,
    status: str,
    repo: str,
    branch: str = "",
    files_scope: str | Iterable[str] | None = None,
    context_ids: str | Iterable[str] | None = None,
    next_action: str = "",
    pr_url: str = "",
) -> dict[str, Any]:
    """Build the canonical handoff metadata shape.

    The metadata is intentionally JSONB-friendly and append-only. Status updates
    should create follow-up memories with the same ``task_id`` rather than
    mutating older handoffs.
    """
    metadata: dict[str, Any] = {
        "kind": HANDOFF_KIND,
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "task_id": task_id,
        "from_agent": from_agent,
        "to_agent": to_agent,
        "phase": phase,
        "status": status,
        "repo": repo,
        "branch": branch,
        "files_scope": _split_csv(files_scope),
        "context_ids": _split_csv(context_ids),
        "next_action": next_action,
    }
    if pr_url:
        metadata["pr_url"] = pr_url
    return metadata


def _metadata_json(metadata: dict[str, Any]) -> str:
    return json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True)


def render_agent_handoff_prompt(
    *,
    task_id: str,
    from_agent: str,
    to_agent: str,
    phase: str = "planning",
    status: str = "ready_for_implementation",
    repo: str = "",
    branch: str = "",
    files_scope: str = "",
    context_ids: str = "",
    next_action: str = "",
    summary: str = "",
    pr_url: str = "",
) -> str:
    """Render an MCP prompt that teaches an agent to create a handoff memory."""
    metadata = build_handoff_metadata(
        task_id=task_id,
        from_agent=from_agent,
        to_agent=to_agent,
        phase=phase,
        status=status,
        repo=repo,
        branch=branch,
        files_scope=files_scope,
        context_ids=context_ids,
        next_action=next_action,
        pr_url=pr_url,
    )
    routing_summary = summary or f"handoff:{task_id} {status} -> {to_agent}"
    routing_key = f"handoff:{task_id} {status} -> {to_agent}"
    scoped_files = metadata["files_scope"] or ["(none specified)"]
    context_hint = metadata["context_ids"] or ["(none specified)"]

    return f"""Create a Synapto cross-agent handoff memory.

task_id: `{task_id}`
from_agent: `{from_agent}`
to_agent: `{to_agent}`
phase: `{phase}`
status: `{status}`
repo: `{repo or '(not specified)'}`
branch: `{branch or '(not specified)'}`
summary: `{routing_summary}`
routing_key: `{routing_key}`

Call `remember` exactly once with:

- memory_type: `project`
- depth_layer: `working`
- summary: `{routing_summary}`
- extract_entities: `true`
- metadata:

```json
{_metadata_json(metadata)}
```

The `content` must be a complete human-readable state packet for the next
agent. Include:

- Goal and current state.
- Decisions already made and why.
- Files or areas in scope: {", ".join(scoped_files)}.
- Relevant memory IDs to fetch with `get_memory`: {", ".join(context_hint)}.
- Concrete next action: {next_action or "(not specified)"}.
- Validation already run and validation still needed.
- Open questions or blockers.

Coordination rules:

- Do not edit files outside `files_scope` unless the user explicitly expands the scope.
- Treat this as advisory coordination, not a hard lock.
- If you receive this handoff, first run `recall` with the task id, then call
  `get_memory(id)` for the handoff and any `context_ids` before acting.
- When you finish, append a new handoff/update memory with the same `task_id`
  instead of mutating the original memory.
"""


def render_handoff_inbox_prompt(
    *,
    agent: str,
    tenant: str | None = None,
    task_id: str = "",
    status: str = "ready_for_implementation",
    limit: int | str = DEFAULT_HANDOFF_LIMIT,
) -> str:
    """Render an MCP prompt that teaches an agent to find assigned handoffs."""
    safe_limit = _coerce_limit(limit)
    query_parts = [f"kind:{HANDOFF_KIND}", f"to_agent:{agent}", f"status:{status}"]
    if task_id:
        query_parts.append(f"task_id:{task_id}")
    query = " ".join(query_parts)
    tenant_arg = f"tenant=`{tenant}`" if tenant else "tenant=`<default>`"

    return f"""Find Synapto handoffs assigned to this agent using two-stage retrieval.

1. Call `recall` with:
   - query: `{query}`
   - {tenant_arg}
   - depth_layer=`working`
   - limit={safe_limit}
   - preview_chars=200

2. Inspect the returned IDs and call `get_memory(id)` for the most relevant
   handoff. If the metadata has `context_ids`, call `get_memories(ids=[...])`
   or `get_memory(id)` for those supporting memories too.

3. Before editing, respect the handoff scope:
   - Work only inside `files_scope` unless the user expands it.
   - Treat existing handoffs as advisory claims, not hard locks.
   - If another active handoff appears to own the same files, ask the user or
     append a blocker handoff instead of racing.

4. After acting, append a follow-up memory with `remember`:
   - metadata.kind=`{HANDOFF_KIND}`
   - metadata.schema_version={HANDOFF_SCHEMA_VERSION}
   - same task_id
   - from_agent=`{agent}`
   - status=`ready_for_review`, `blocked`, or `completed`
"""


__all__ = [
    "DEFAULT_HANDOFF_LIMIT",
    "HANDOFF_KIND",
    "HANDOFF_SCHEMA_VERSION",
    "build_handoff_metadata",
    "render_agent_handoff_prompt",
    "render_handoff_inbox_prompt",
]
