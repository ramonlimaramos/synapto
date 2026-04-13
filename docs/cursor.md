# Synapto + Cursor

## Setup

1. Install Synapto and initialize:

```bash
pip install synapto
createdb synapto && psql -d synapto -c "CREATE EXTENSION vector;"
synapto init
```

2. Add to `.cursor/mcp.json` in your project root:

**Recommended (auto-updates via uvx):**

```json
{
  "mcpServers": {
    "synapto": {
      "command": "uvx",
      "args": ["synapto", "serve"],
      "env": {
        "SYNAPTO_DEFAULT_TENANT": "my-project"
      }
    }
  }
}
```

> **Why uvx?** It resolves the latest version from PyPI each time Cursor starts the server. No manual `pip install --upgrade` needed.

3. Restart Cursor. Synapto tools will be available to the AI agent.

## Usage

Cursor's AI will automatically use `remember` and `recall` tools when appropriate. You can also prompt:

- "Store this pattern in memory for future reference"
- "Search memory for how we handle authentication"
- "What entities are related to the user service?"
