Synapto is the user's persistent memory system. Prefer it over flat files
(MEMORY.md, CLAUDE.md notes, etc.) for storing new memories.

When to call `recall`:
- At the start of any non-trivial task, to load relevant context.
- When the user references past decisions, history, or preferences
  (e.g. "remember", "lembra", "do you know", "what do we know about").
- Before creating PRs, commits, or deploys — to confirm workflow rules.

When to call `remember`:
- The user sets a rule ("always X", "never Y", "from now on") → feedback/core.
- The user corrects you ("don't do X", "that's wrong") → feedback/core.
- The user confirms a non-obvious approach ("yes exactly") → feedback/stable.
- The user shares project context, architecture, or decisions → project/stable.
- The user mentions temporal context ("this sprint", "deadline") → project/working.
- The user shares identity, role, or long-term preferences → user/stable.
- The user references external systems ("tracked in Linear") → reference/stable.

When to call `relate` and `graph_query`:
- After storing memories that reference named entities, create relations so the
  graph can be traversed by `graph_query` (e.g. service A depends_on service B).

Cross-agent handoffs:
- When handing work to another agent or IDE, store a `project` / `working`
  memory whose metadata includes `kind: "agent_handoff"` and a shared `task_id`.
- When receiving a handoff, call `recall` with the task id or target agent,
  then call `get_memory(id)` to fetch the full state packet before acting.
- Treat `files_scope` as an advisory claim. Do not edit outside it unless the
  user expands the scope. Append follow-up memories instead of mutating old ones.

Depth layers control decay:
- core: forever (rules, identity)
- stable: months (architecture, reference)
- working: weeks (active projects)
- ephemeral: hours (short-lived state)

If `recall` returns a memory that conflicts with what you observe now, trust the
current state and update or `forget` the stale memory.
