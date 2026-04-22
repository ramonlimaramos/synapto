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
