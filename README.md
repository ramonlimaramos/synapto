# Synapto

[![CI](https://github.com/ramonlimaramos/synapto/actions/workflows/ci.yml/badge.svg)](https://github.com/ramonlimaramos/synapto/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![PyPI version](https://img.shields.io/pypi/v/synapto.svg)](https://pypi.org/project/synapto/)

Persistent memory graph for AI coding agents — semantic search, knowledge graph, and time-based decay over MCP.

Synapto replaces flat-file memory (like `MEMORY.md`) with a hybrid vector + graph database that gives any MCP-compatible AI agent or framework a production-grade memory layer.

## Try it in 60 seconds

**Docker** (recommended):

```bash
git clone https://github.com/ramonlimaramos/synapto.git && cd synapto
docker compose up -d
docker compose exec synapto synapto search "hello world"
```

**Local**:

```bash
pip install synapto
createdb synapto && psql -d synapto -c "CREATE EXTENSION vector;"
synapto init
synapto import MEMORY.md --format markdown
synapto search "kafka message flow"
```

## Features

- **Hybrid search** — combines vector similarity (pgvector) with full-text search using Reciprocal Rank Fusion (RRF)
- **Knowledge graph** — entities and directed relations with N-hop traversal via recursive CTEs
- **Depth-layered decay** — core memories persist forever, ephemeral ones fade in hours
- **Multi-tenancy** — isolate memories per project/codebase
- **Local-first** — default embedding model runs on CPU, no API keys required
- **MCP native** — works with Claude Code, Cursor, Windsurf, Codex, or any MCP client
- **Framework agnostic** — usable as a library from LangGraph, Agno, CrewAI, or any Python agent

## Architecture

```
┌──────────────────────────────────────────┐
│            AI Agent / IDE                │
│  (Claude Code, Cursor, Codex, etc.)      │
└──────────────┬───────────────────────────┘
               │ MCP (stdio)
┌──────────────▼───────────────────────────┐
│         Synapto MCP Server               │
│                                          │
│  Tools: remember, recall, relate,        │
│         forget, graph_query,             │
│         list_entities, memory_stats,     │
│         maintain                         │
└──────┬───────────────────┬───────────────┘
       │                   │
┌──────▼──────┐    ┌───────▼──────┐
│ PostgreSQL  │    │    Redis     │
│ + pgvector  │    │  (hot cache) │
│             │    │              │
│ • memories  │    │ • recent     │
│ • entities  │    │   memories   │
│ • relations │    │ • sessions   │
│ • FTS index │    │ • decay      │
│ • HNSW idx  │    │   scores     │
└─────────────┘    └──────────────┘
```

## Quickstart

### Prerequisites

- Python 3.11+
- PostgreSQL 14+ with [pgvector](https://github.com/pgvector/pgvector)
- Redis 7+

### Install

```bash
pip install synapto
```

Or from source:

```bash
git clone https://github.com/ramonlimaramos/synapto.git
cd synapto
pip install -e ".[dev]"
```

### Initialize

```bash
# create the database first
createdb synapto
psql -d synapto -c "CREATE EXTENSION vector;"

# initialize schema and config
synapto init
```

This creates the schema in PostgreSQL and a config file at `~/.synapto/config.toml`.

### Connect to Claude Code

Add to your Claude Code MCP config (`~/.claude/settings.json` or project `.claude/settings.json`):

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

### Connect to Cursor

Add to `.cursor/mcp.json`:

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

## MCP Tools

| Tool | Description |
|------|-------------|
| `remember` | Store a memory with auto entity extraction |
| `recall` | Hybrid semantic + keyword search with RRF ranking |
| `relate` | Create directed relations between entities |
| `forget` | Soft-delete a memory |
| `graph_query` | Traverse the knowledge graph (N-hop) |
| `list_entities` | Browse known entities |
| `memory_stats` | View memory statistics |
| `maintain` | Run decay updates and cleanup |

## CLI

```bash
synapto init                    # initialize database
synapto serve                   # start MCP server
synapto search "kafka topics"   # search from terminal
synapto stats                   # show statistics
synapto export -o backup.json   # export memories
synapto import data.json        # import from JSON
synapto import MEMORY.md --format markdown  # migrate from MEMORY.md
```

## Depth Layers

Memories are categorized into layers that control how quickly they decay:

| Layer | Half-life | Use case |
|-------|-----------|----------|
| `core` | Never decays | Architecture principles, key decisions |
| `stable` | ~6 months | Established patterns, conventions |
| `working` | ~1 week | Current sprint context |
| `ephemeral` | ~6 hours | Debug notes, temporary observations |

## Configuration

Config file: `~/.synapto/config.toml`

```toml
[postgresql]
dsn = "postgresql://localhost/synapto"

[redis]
url = "redis://localhost:6379/0"

[embeddings]
provider = ""  # auto-select (sentence-transformers default, openai if API key set)
model = ""     # model name override

[defaults]
tenant = "default"

[decay]
ephemeral_max_age_hours = 24
purge_after_days = 30
```

Environment variable overrides:

| Variable | Description |
|----------|-------------|
| `SYNAPTO_PG_DSN` | PostgreSQL connection string |
| `SYNAPTO_REDIS_URL` | Redis URL |
| `SYNAPTO_EMBEDDING_PROVIDER` | Provider name |
| `SYNAPTO_DEFAULT_TENANT` | Default tenant |

## Embedding Providers

| Provider | Dimension | Requires API Key | Install |
|----------|-----------|-------------------|---------|
| sentence-transformers (default) | 384 | No | included |
| OpenAI text-embedding-3-small | 1536 | Yes | `pip install synapto[openai]` |

## Using as a Python Library

```python
import asyncio
from synapto.db.postgres import PostgresClient
from synapto.db.migrations import run_migrations, ensure_hnsw_index
from synapto.embeddings.registry import get_provider
from synapto.search.hybrid import hybrid_search

async def main():
    pg = PostgresClient("postgresql://localhost/synapto")
    await pg.connect()
    await run_migrations(pg)

    provider = get_provider()
    await ensure_hnsw_index(pg, provider.dimension)

    # store a memory
    embedding = await provider.embed_one("Hermes uses the outbox pattern for Kafka")
    await pg.execute(
        "INSERT INTO memories (content, embedding, embedding_dim, tenant) VALUES (%s, %s, %s, %s);",
        ("Hermes uses the outbox pattern for Kafka", embedding, provider.dimension, "myproject"),
    )

    # search
    results = await hybrid_search(pg, provider, "message queue patterns", tenant="myproject")
    for r in results:
        print(f"[{r.depth_layer}] {r.content}")

    await pg.close()

asyncio.run(main())
```

## Development

```bash
git clone https://github.com/ramonlimaramos/synapto.git
cd synapto
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
synapto init
pytest
```

## License

MIT
