# Synapto + Claude Code

## Setup

1. Install Synapto and initialize the database:

```bash
pip install synapto
createdb synapto && psql -d synapto -c "CREATE EXTENSION vector;"
synapto init
```

2. Add to your Claude Code MCP config.

**Global** (`~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "synapto": {
      "command": "synapto",
      "args": ["serve"]
    }
  }
}
```

**Per-project** (`.claude/settings.json` in your repo root):

```json
{
  "mcpServers": {
    "synapto": {
      "command": "synapto",
      "args": ["serve"],
      "env": {
        "SYNAPTO_DEFAULT_TENANT": "my-project"
      }
    }
  }
}
```

3. Restart Claude Code. Synapto tools will appear in your tool list.

## Usage Examples

Claude Code will automatically use Synapto tools. You can also ask directly:

- "Remember that Hermes uses the outbox relay pattern for Kafka"
- "What do you know about our Kafka architecture?"
- "What depends on the agent.trigger topic?"
- "Show me memory stats"

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
