# Synapto

[![CI](https://github.com/ramonlimaramos/synapto/actions/workflows/ci.yml/badge.svg)](https://github.com/ramonlimaramos/synapto/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![PyPI version](https://img.shields.io/pypi/v/synapto.svg)](https://pypi.org/project/synapto/)

Persistent memory graph for AI coding agents — 3-way hybrid search, compositional algebra, knowledge graph, and time-based decay over MCP.

Synapto replaces flat-file memory (like `MEMORY.md`) with a hybrid vector + graph database that gives any MCP-compatible AI agent or framework a production-grade memory layer. It's the only memory server that can answer "find everything where Kafka AND Hermes both play a structural role" — not via keyword matching, but via algebraic vector operations that no embedding database can do.

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

## Why Synapto

Most AI memory solutions do one thing: vector similarity search. Synapto combines **three search signals** and adds capabilities no other memory server has:

```
┌─────────────────────────────────────────────────────────────┐
│                    recall("kafka patterns")                  │
│                                                             │
│  Signal 1: Vector Similarity (pgvector)                     │
│    → "Hermes uses outbox relay for Kafka" scores 0.89       │
│                                                             │
│  Signal 2: Full-Text Search (tsvector + BM25)               │
│    → matches "Kafka" keyword, boosts rank                   │
│                                                             │
│  Signal 3: HRR Compositional Algebra  ← only in Synapto    │
│    → Kafka is structurally bound as an entity in this fact  │
│    → algebraic extraction confirms structural role          │
│                                                             │
│  Final: RRF(signals) × decay × trust × depth_boost         │
└─────────────────────────────────────────────────────────────┘
```

### What HRR enables that embeddings can't

| Capability | Embeddings | Synapto HRR |
|-----------|-----------|-------------|
| "Find memories about Kafka" | Keyword/similarity match | Algebraic structural role detection |
| "Find memories about Kafka AND Hermes together" | Hope both words appear nearby | Vector-space JOIN with AND semantics |
| "Find contradictory memories" | Not possible | Entity overlap + content divergence analysis |
| "Extract which entities are involved in a fact" | Not possible | `unbind(fact, role) → entity` |
| Trust-based ranking | Not possible | Asymmetric feedback loop (+0.05 / -0.10) |

## Features

- **3-way hybrid search** — vector similarity + full-text + HRR compositional algebra, fused via Reciprocal Rank Fusion
- **Holographic Reduced Representations** — algebraic `probe`, `reason` (multi-entity JOIN), and `contradict` (memory hygiene)
- **Trust scoring** — asymmetric feedback loop that demotes bad memories 2x faster than it promotes good ones
- **Knowledge graph** — entities and directed relations with N-hop traversal via recursive CTEs
- **Depth-layered decay** — core memories persist forever, ephemeral ones fade in hours
- **Contradiction detection** — automatically find memory pairs that share entities but disagree
- **Multi-tenancy** — isolate memories per project/codebase
- **Local-first** — default embedding model runs on CPU, no API keys required
- **Versioned migrations** — SQL files with up/down sections, checksums, and rollback support
- **MCP native** — works with Claude Code, Cursor, Windsurf, Codex, or any MCP client
- **Framework agnostic** — usable as a library from LangGraph, Agno, CrewAI, or any Python agent

## Architecture

```
┌──────────────────────────────────────────────┐
│              AI Agent / IDE                  │
│    (Claude Code, Cursor, Codex, etc.)        │
└──────────────┬───────────────────────────────┘
               │ MCP (stdio / SSE)
┌──────────────▼───────────────────────────────┐
│           Synapto MCP Server                 │
│                                              │
│  remember → embedding + HRR vector + entities│
│  recall   → 3-way RRF (vector+FTS+HRR)      │
│  relate   → knowledge graph edges            │
│  trust_feedback → asymmetric scoring         │
│  find_contradictions → memory hygiene        │
│  graph_query, forget, maintain, ...          │
└──────┬───────────────────┬───────────────────┘
       │                   │
┌──────▼──────┐    ┌───────▼──────┐
│ PostgreSQL  │    │    Redis     │
│ + pgvector  │    │  (hot cache) │
│             │    │              │
│ • memories  │    │ • recent     │
│ • entities  │    │   memories   │
│ • relations │    │ • sessions   │
│ • HRR vecs  │    │ • decay      │
│ • FTS + HNSW│    │   scores     │
│ • mem banks │    │              │
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

# or interactive setup
synapto init --interactive
```

This runs all migrations and creates a config file at `~/.synapto/config.toml`.

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
| `remember` | Store a memory with auto entity extraction + HRR vector |
| `recall` | 3-way hybrid search (vector + FTS + HRR) with RRF ranking |
| `relate` | Create directed relations between entities |
| `forget` | Soft-delete a memory |
| `trust_feedback` | Adjust memory trust score (helpful +0.05 / unhelpful -0.10) |
| `find_contradictions` | Detect memory pairs that share entities but disagree |
| `graph_query` | Traverse the knowledge graph (N-hop) |
| `list_entities` | Browse known entities |
| `memory_stats` | View memory statistics |
| `maintain` | Run decay updates and cleanup |

## CLI

```bash
synapto init                    # initialize database
synapto init -i                 # interactive setup
synapto serve                   # start MCP server
synapto search "kafka topics"   # search from terminal
synapto stats                   # show statistics
synapto doctor                  # check system health
synapto migrate status          # show migration status
synapto migrate up              # apply pending migrations
synapto migrate down --to 1     # rollback to version 1
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
from synapto.hrr.retrieval import probe, reason

async def main():
    pg = PostgresClient("postgresql://localhost/synapto")
    await pg.connect()
    await run_migrations(pg)

    provider = get_provider()
    await ensure_hnsw_index(pg, provider.dimension)

    # 3-way hybrid search (vector + FTS + HRR)
    results = await hybrid_search(pg, provider, "message queue patterns", tenant="myproject")
    for r in results:
        print(f"[{r.depth_layer}] trust={r.trust_score:.2f} {r.content}")

    # HRR compositional search: find where "kafka" plays a structural role
    hrr_results = await probe(pg, "kafka", tenant="myproject")

    # multi-entity JOIN: memories about kafka AND hermes together
    join_results = await reason(pg, ["kafka", "hermes"], tenant="myproject")

    await pg.close()

asyncio.run(main())
```

## Documentation

| Doc | Description |
|-----|-------------|
| [HRR](docs/hrr.md) | Holographic Reduced Representations — algebra, 3-way search, compositional queries |
| [Trust Scoring](docs/trust-scoring.md) | Asymmetric feedback loop and contradiction workflow |
| [Migrations](docs/migrations.md) | Versioned SQL migration system with rollback support |
| [Claude Code](docs/claude-code.md) | Integration guide for Claude Code |
| [Cursor](docs/cursor.md) | Integration guide for Cursor |
| [LangGraph](docs/langgraph.md) | Using Synapto as a LangGraph tool |
| [Agno](docs/agno.md) | Using Synapto with Agno agents |

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
