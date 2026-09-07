Synapto is the user's persistent memory system. Prefer it over flat files
(MEMORY.md, CLAUDE.md notes, etc.) for storing new memories.

Transport diagnostics:
- Call `ping` first when checking MCP transport health. It only proves the MCP
  connection is alive and does not touch PostgreSQL, Redis, or embeddings.
- If tools fail with `Transport closed`, treat it as a closed MCP client
  transport and ask the user to restart/reconnect the MCP client instead of
  writing fallback flat-file memories.

When to call `recall`:
- At the start of any non-trivial task, to load relevant context.
- When the user references past decisions, history, or preferences
  (e.g. "remember", "lembra", "do you know", "what do we know about").
- Before creating PRs, commits, or deploys — to confirm workflow rules.
- Pass `domain=` (skill/repo/language bounded context, e.g. `domain=python`,
  `domain=jerry-workday`) to load only that domain's governed context without
  semantic query guessing.

When to call `remember`:
- The user sets a rule ("always X", "never Y", "from now on") → feedback/core.
- The user corrects you ("don't do X", "that's wrong") → feedback/core.
- The user confirms a non-obvious approach ("yes exactly") → feedback/stable.
- The user shares project context, architecture, or decisions → project/stable.
- The user mentions temporal context ("this sprint", "deadline") → project/working.
- The user shares identity, role, or long-term preferences → user/stable.
- The user references external systems ("tracked in Linear") → reference/stable.
- Durable skill/repo/language knowledge (patterns, conventions, tooling rules
  tied to one bounded context) → store with `domain=` (e.g. `domain=python`,
  `domain=synapto`) so skills fetch governed context from Synapto instead of
  flat files.

When to call `relate` and `graph_query`:
- After storing memories that reference named entities, create relations so the
  graph can be traversed by `graph_query` (e.g. service A depends_on service B).

Cross-agent handoffs:
- Treat natural-language requests like "leave a handoff for Claude" or
  "continue from this Synapto handoff ID" as handoff workflows. Do the
  structured memory work under the hood instead of asking the user to build
  metadata payloads.
- A task has ONE handoff packet. Before writing, look it up with
  `recall(task_id, metadata_filter={"kind": "handoff", "task_id": ...})` (and
  `kind: "agent_handoff"` for packets whose `schema_version` is 1); extend an existing
  packet with `update_memory(append=..., metadata_patch={"status": ...})`. Only
  when none exists, store a `project` memory with `subtype: "handoff"`,
  `origin: "agent"`, `scopes: ["area:<discipline>"]`, tenant omitted (derived)
  or `<owner>/workspace` for a multi-repo task, and the shared `task_id`.
- Return the packet ID to the user so another agent can continue with
  `get_memory(id)`.
- When receiving a handoff, look the packet up by `metadata_filter`, then call
  `get_memory(id)` to read the full state and any `context_ids` before acting.
- Treat `files_scope` as an advisory claim. Do not edit outside it unless the
  user expands the scope. A second memory for the same `task_id` is a defect.
- When the task is done, set `status: "done"` and `depth_layer: "ephemeral"`
  in one `update_memory` call so maintenance retires the packet.

Depth layers control decay:
- core: forever (rules, identity)
- stable: months (architecture, reference)
- working: weeks (active projects)
- ephemeral: hours (short-lived state)

If `recall` returns a memory that conflicts with what you observe now, trust the
current state and call `update_memory` or `forget` for the stale memory.
