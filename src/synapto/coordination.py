"""Cross-agent coordination helpers.

Handoffs are ordinary project memories, which keeps coordination portable
across MCP clients: agents use ``recall`` with a ``metadata_filter`` to find
the packet, ``get_memory`` to read it, ``update_memory`` to extend it and
``remember`` only to create it.

**One packet per task.** A task's handoff is a single memory, looked up by
``metadata.kind`` and ``metadata.task_id`` and extended with
``update_memory(append=…)`` as its state advances. The earlier rule — "append
a new memory instead of mutating the original" — produced four near-identical
memories for one task, each a stale snapshot ranking against the others, so
the receiver could not tell which one was current. A single packet has one
``status``, and its history is the appended log inside it.

The kind was renamed from ``agent_handoff`` to ``handoff`` when the packet rule
changed; readers accept both for one release so existing packets stay
discoverable.
"""

from __future__ import annotations

import json
from typing import Any

from synapto.scopes import ScopeRef

HANDOFF_KIND = "handoff"
LEGACY_HANDOFF_KIND = "agent_handoff"
HANDOFF_SCHEMA_VERSION = 2
HANDOFF_SUBTYPE = "handoff"
HANDOFF_ORIGIN = "agent"
DEFAULT_HANDOFF_AREA = "software-engineering"
HANDOFF_DONE_STATUS = "done"
DEFAULT_HANDOFF_LIMIT = 10
_FORBIDDEN_INLINE_CHARS = ("\n", "\r", "`")
_MAX_INLINE_LEN = 200
_MAX_TEXT_LEN = 2_000


def _safe_inline(value: str | None, *, name: str) -> str:
    text = "" if value is None else str(value).strip()
    if any(char in text for char in _FORBIDDEN_INLINE_CHARS):
        raise ValueError(f"{name!r} must not contain newlines or backticks")
    if len(text) > _MAX_INLINE_LEN:
        raise ValueError(f"{name!r} exceeds {_MAX_INLINE_LEN} chars")
    return text


def _canonical_area(area: str | None) -> str:
    """Return the ``area`` scope key the packet will carry, or the default when none was given.

    The value goes through the same grammar ``remember`` applies to a scope key, so a
    non-canonical area fails here, at the prompt, with the message naming the canonical
    form — instead of failing later at the agent's ``remember`` call, after the packet
    text was composed around it. Nothing is repaired: an empty value selects the
    default, any other value must already be canonical. One pattern match, O(len(area)).
    """
    if not area:
        return DEFAULT_HANDOFF_AREA
    return ScopeRef.parse("area", area).scope_key


def _safe_text(value: str | None, *, name: str) -> str:
    text = "" if value is None else str(value).strip()
    if len(text) > _MAX_TEXT_LEN:
        raise ValueError(f"{name!r} exceeds {_MAX_TEXT_LEN} chars")
    return text


def _split_csv(value: str | None, *, name: str = "value") -> list[str]:
    if not value:
        return []
    return [_safe_inline(item, name=name) for item in value.split(",") if item.strip()]


def _coerce_limit(value: int | str | None) -> int:
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
    files_scope: str | None = None,
    context_ids: str | None = None,
    next_action: str = "",
    pr_url: str = "",
) -> dict[str, Any]:
    """Build the canonical handoff metadata shape.

    ``kind`` and ``task_id`` are the lookup key: a receiver finds the packet
    with ``metadata_filter={"kind": "handoff", "task_id": …}`` rather than by
    ranking. The remaining fields are the state the sender declares and the
    receiver patches in place with ``update_memory(metadata_patch=…)``; a
    ``repo`` is a fact about the packet and lives here, not in scopes.
    """
    safe_pr_url = _safe_inline(pr_url, name="pr_url")
    metadata: dict[str, Any] = {
        "kind": HANDOFF_KIND,
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "task_id": _safe_inline(task_id, name="task_id"),
        "from_agent": _safe_inline(from_agent, name="from_agent"),
        "to_agent": _safe_inline(to_agent, name="to_agent"),
        "phase": _safe_inline(phase, name="phase"),
        "status": _safe_inline(status, name="status"),
        "repo": _safe_inline(repo, name="repo"),
        "branch": _safe_inline(branch, name="branch"),
        "files_scope": _split_csv(files_scope, name="files_scope"),
        "context_ids": _split_csv(context_ids, name="context_ids"),
        "next_action": _safe_text(next_action, name="next_action"),
    }
    if safe_pr_url:
        metadata["pr_url"] = safe_pr_url
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
    area: str = DEFAULT_HANDOFF_AREA,
) -> str:
    """Render an MCP prompt that teaches an agent to create or extend a handoff packet."""
    safe_area = _canonical_area(area)
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
    safe_summary = _safe_inline(summary, name="summary")
    task_id = metadata["task_id"]
    from_agent = metadata["from_agent"]
    to_agent = metadata["to_agent"]
    phase = metadata["phase"]
    status = metadata["status"]
    repo = metadata["repo"]
    branch = metadata["branch"]
    next_action = metadata["next_action"]
    routing_summary = safe_summary or f"handoff:{task_id} {status} -> {to_agent}"
    routing_key = f"handoff:{task_id} {status} -> {to_agent}"
    scoped_files = metadata["files_scope"] or ["(none specified)"]
    context_hint = metadata["context_ids"] or ["(none specified)"]
    next_action_display = json.dumps(next_action or "(not specified)", ensure_ascii=False)

    lookup_filter = json.dumps({"kind": HANDOFF_KIND, "task_id": task_id}, ensure_ascii=False)
    legacy_filter = json.dumps({"kind": LEGACY_HANDOFF_KIND, "task_id": task_id}, ensure_ascii=False)
    patch_json = json.dumps(
        {"status": status, "phase": phase, "from_agent": from_agent, "to_agent": to_agent, "next_action": next_action},
        ensure_ascii=False,
    )

    return f"""Create or extend the Synapto handoff packet for one task.

task_id: `{task_id}`
from_agent: `{from_agent}`
to_agent: `{to_agent}`
phase: `{phase}`
status: `{status}`
repo: `{repo or '(not specified)'}`
branch: `{branch or '(not specified)'}`
summary: `{routing_summary}`
routing_key: `{routing_key}`

A task has ONE handoff packet. Look it up before writing:

1. `recall("{task_id}", metadata_filter={lookup_filter})` — also with
   `tenant="<owner>/workspace"` when the task spans repositories, and with
   `metadata_filter={legacy_filter}` for packets written before the rename.
2. If a packet exists, extend it and stop — never write a sibling:

```text
update_memory(
  memory_id="<packet id>",
  append="\n\n## <date> — {phase}\n<what changed since the last entry, what is next, blockers>",
  metadata_patch={patch_json},
)
```

3. Only when no packet exists, call `remember` exactly once with:

- memory_type: `project`
- subtype: `{HANDOFF_SUBTYPE}`
- depth_layer: `working`
- origin: `{HANDOFF_ORIGIN}` (an agent wrote this, not the user)
- scopes: `["area:{safe_area}"]` — scopes are conditions the reader must
  name, so the area is the only one; the repository stays in `metadata.repo`
- tenant: omit it inside the repository (derived from the git remote); pass
  `"<owner>/workspace"` only for a packet that spans repositories
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
- Concrete next action: {next_action_display}.
- Validation already run and validation still needed.
- Open questions or blockers.

Coordination rules:

- Do not edit files outside `files_scope` unless the user explicitly expands the scope.
- Treat this as advisory coordination, not a hard lock.
- If you receive this handoff, look the packet up by `metadata_filter`, then
  call `get_memory(id)` for it and any `context_ids` before acting.
- When you finish, extend the SAME packet with `update_memory(append=…,
  metadata_patch={{"status": …}})`. A second memory for the same `task_id` is
  a defect. When the task is done, set `status` to `{HANDOFF_DONE_STATUS}`
  and `depth_layer` to `ephemeral` in the same call so maintenance retires it.
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
    agent = _safe_inline(agent, name="agent")
    tenant = _safe_inline(tenant, name="tenant")
    task_id = _safe_inline(task_id, name="task_id")
    status = _safe_inline(status, name="status")
    safe_limit = _coerce_limit(limit)
    query = task_id or "handoff"
    lookup: dict[str, Any] = {"kind": HANDOFF_KIND, "to_agent": agent, "status": status}
    if task_id:
        lookup["task_id"] = task_id
    legacy = dict(lookup, kind=LEGACY_HANDOFF_KIND)
    lookup_json = json.dumps(lookup, ensure_ascii=False)
    legacy_json = json.dumps(legacy, ensure_ascii=False)
    tenant_arg = (
        f"tenant=`{tenant}`" if tenant else "tenant omitted (the repository's), then tenant=`<owner>/workspace`"
    )

    return f"""Find the handoff packets assigned to this agent by metadata, not by ranking.

A task has ONE packet: `metadata.kind` `{HANDOFF_KIND}` (or `{LEGACY_HANDOFF_KIND}`
for packets written before the rename) with a shared `task_id`. `metadata_filter`
is an exact match, so the result is the packet, not a candidate list.

1. Call `recall` with:
   - query: `{query}`
   - {tenant_arg}
   - metadata_filter=`{lookup_json}`
   - limit={safe_limit}
   - preview_chars=200
   Repeat with metadata_filter=`{legacy_json}` for older packets.

2. Call `get_memory(id)` for each packet returned. If the metadata has
   `context_ids`, call `get_memories(ids=[...])` for those supporting
   memories too.

3. Before editing, respect the packet's scope:
   - Work only inside `files_scope` unless the user expands it.
   - Treat the packet as an advisory claim, not a hard lock.
   - If another active packet appears to own the same files, ask the user or
     record a blocker on this packet instead of racing.

4. After acting, extend the SAME packet — never write a sibling:

```text
update_memory(
  memory_id="<packet id>",
  append="\\n\\n## <date> — <phase>\\n<what changed, what is next, blockers>",
  metadata_patch={{"status": "ready_for_review" | "blocked" | "{HANDOFF_DONE_STATUS}",
                  "from_agent": "{agent}", "to_agent": "<next agent>"}},
)
```

   When the task is done, pass `depth_layer="ephemeral"` in the same call so
   maintenance retires the packet.
"""


__all__ = [
    "DEFAULT_HANDOFF_AREA",
    "DEFAULT_HANDOFF_LIMIT",
    "HANDOFF_DONE_STATUS",
    "HANDOFF_KIND",
    "HANDOFF_ORIGIN",
    "HANDOFF_SCHEMA_VERSION",
    "HANDOFF_SUBTYPE",
    "LEGACY_HANDOFF_KIND",
    "build_handoff_metadata",
    "render_agent_handoff_prompt",
    "render_handoff_inbox_prompt",
]
