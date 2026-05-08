# Synapto + Cursor

## Setup

1. Install Synapto and initialize:

```bash
pip install synapto
createdb synapto && psql -d synapto -c "CREATE EXTENSION vector;"
synapto init
```

2. Add to `.cursor/mcp.json` in your project root:

**Recommended (auto-updates on every restart):**

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

> **Why `--refresh`?** Without it, `uvx` reuses the cached environment across restarts, so a new Synapto release on PyPI will not be picked up until the cache expires or you run `uv cache clean synapto` manually. `--refresh` tells `uv` to re-resolve the package on every launch, adding 1–3 seconds to startup in exchange for "always on the latest version". Drop the flag (or pin a version like `"synapto==0.2.0"`) once you want to freeze a known-good build.

3. Restart Cursor. Synapto tools will be available to the AI agent.

## Usage

Cursor's AI will automatically use `remember` and `recall` tools when appropriate. You can also prompt:

- "Store this pattern in memory for future reference"
- "Search memory for how we handle authentication"
- "What entities are related to the user service?"

## Cross-Agent Handoffs

If your Cursor MCP client exposes Synapto prompts, use `agent_handoff` to create
a handoff and `handoff_inbox` to receive one. If prompts are not surfaced, ask
Cursor to follow the same workflow manually:

```text
recall(
  "kind:agent_handoff to_agent:cursor status:ready_for_implementation",
  depth_layer="working",
  preview_chars=200
)
get_memory("<handoff-id>")
```

Handoffs are normal project memories with structured metadata, so they remain
portable across Codex, Claude Code, Cursor, and other MCP clients. See
[Cross-agent handoffs](handoffs.md) for the full schema.
