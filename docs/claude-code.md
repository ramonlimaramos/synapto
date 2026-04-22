# Synapto + Claude Code

## Setup

1. Install Synapto and initialize the database:

```bash
pip install synapto
createdb synapto && psql -d synapto -c "CREATE EXTENSION vector;"
synapto init
```

2. Add to your Claude Code MCP config.

**Recommended (auto-updates on every restart)** — add to `~/.claude/.mcp.json`:

```json
{
  "mcpServers": {
    "synapto": {
      "command": "uvx",
      "args": ["--refresh", "synapto", "serve"]
    }
  }
}
```

**Per-project with tenant isolation** (`.claude/settings.json` in your repo root):

```json
{
  "mcpServers": {
    "synapto": {
      "command": "uvx",
      "args": ["--refresh", "synapto", "serve"],
      "env": {
        "SYNAPTO_DEFAULT_TENANT": "my-project"
      }
    }
  }
}
```

> **Why `--refresh`?** Without it, `uvx` reuses the cached environment across restarts, so a new Synapto release on PyPI will not be picked up until the cache expires or you run `uv cache clean synapto` manually. `--refresh` tells `uv` to re-resolve the package on every launch, adding 1–3 seconds to Claude Code's MCP startup in exchange for "always on the latest version" — the right default while Synapto is shipping fast. Drop the flag (or pin a version like `"synapto==0.2.0"`) once you want to freeze a known-good build.

### Upgrading mid-session

If a new Synapto release lands while Claude Code is already running, the existing MCP subprocess keeps using the version it started with. To pick up the new release, **fully quit Claude Code (`Cmd+Q`) and relaunch** — the MCP server is a child process of Claude Code, so a window-close is not enough. With `--refresh` in place, the relaunch will pull the new version automatically.

### Forcing an upgrade without `--refresh`

If you pinned the config without `--refresh` and want a one-time upgrade:

```bash
uv cache clean synapto   # invalidate the cached env
# then relaunch Claude Code
```

3. Restart Claude Code. Synapto tools will appear in your tool list.

## Usage Examples

Claude Code will automatically use Synapto tools. You can also ask directly:

- "Remember that Hermes uses the outbox relay pattern for Kafka"
- "What do you know about our Kafka architecture?"
- "What depends on the agent.trigger topic?"
- "Show me memory stats"

## Memory type alignment

Synapto's `memory_type` categories are **100% compatible** with Claude Code's native
auto-memory types. No translation layer, mapping table, or converter is required
when moving memories between the two systems.

| Type | Claude Code meaning | Synapto meaning |
|------|--------------------|-----------------|
| `user` | Who the user is — role, goals, knowledge, preferences | Same |
| `feedback` | How to work — rules, corrections, confirmed approaches | Same |
| `project` | What the project is — goals, incidents, temporal context | Same |
| `reference` | Where to find things — external systems, links, locations | Same |
| `general` | _(not present — Claude Code has no catch-all)_ | Catch-all for memories that don't fit the four canonical types |

Claude Code defines the same four types in
[`src/memdir/memoryTypes.ts`](https://github.com/anthropics/claude-code) with
identical semantics. Since Synapto mirrors them exactly, any memory Claude Code
produces via its auto-memory flow can be stored in Synapto without modification.

### Frontmatter format

Claude Code's auto-memory files use YAML frontmatter with `name`, `description`,
and `type` fields:

```markdown
---
name: git workflow
description: branching and commit conventions for this project
type: feedback
---

Always branch off `main`, never commit directly. Commit messages must follow …
```

When importing such files into Synapto, the mapping is direct:

| Frontmatter field | Synapto field |
|-------------------|---------------|
| `type` | `memory_type` |
| `description` | `summary` |
| body (after `---`) | `content` |
| _(implicit)_ | `depth_layer` defaults to `stable` for imported files |

This enables the upcoming auto-migration flow ([issue #8](https://github.com/ramonlimaramos/synapto/issues/8)) to ingest existing Claude Code memories with zero transformation.

## Migrating from MEMORY.md

```bash
synapto import ~/.claude/projects/your-project/memory/MEMORY.md --format markdown -t your-project
```

This imports each `## Section` as a separate `stable` memory with full semantic search.

## Multi-Project Setup

Use per-project configs with different tenants to keep memories isolated:

```bash
# In project A's .claude/settings.json
"env": { "SYNAPTO_DEFAULT_TENANT": "project-a" }

# In project B's .claude/settings.json
"env": { "SYNAPTO_DEFAULT_TENANT": "project-b" }
```
