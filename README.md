# Synapto

[![CI](https://github.com/ramonlimaramos/synapto/actions/workflows/ci.yml/badge.svg)](https://github.com/ramonlimaramos/synapto/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![PyPI version](https://img.shields.io/pypi/v/synapto.svg)](https://pypi.org/project/synapto/)

**Your AI agent forgets everything between sessions. Synapto fixes that.**

Flat-file memory (`MEMORY.md`) doesn't scale — no search, no structure, no decay. Synapto gives any MCP-compatible agent a real memory: store once, recall by meaning, watch bad memories fade and good ones persist.

```bash
# remember
"Hermes uses the outbox relay pattern for Kafka"

# recall — weeks later, different session
"How does Hermes handle messaging?"
→ [stable] Hermes uses the outbox relay pattern for Kafka (score=0.94, trust=0.65)
```

Works with Claude Code, Cursor, Windsurf, Codex, LangGraph, Agno, or any MCP client.

## Try it in 60 seconds

**Docker:**

```bash
git clone https://github.com/ramonlimaramos/synapto.git && cd synapto
docker compose up -d
docker compose exec synapto synapto search "hello world"
```

**Local:**

```bash
pip install synapto
createdb synapto && psql -d synapto -c "CREATE EXTENSION vector;"
synapto init
synapto search "hello world"
```

## What it does

**Search** — Ask a question, get the best memory. Behind the scenes, three signals (vector similarity, full-text, and compositional algebra) are fused into one score. You just call `recall`.

**Graph** — Entities are auto-extracted and linked. Ask "what depends on Kafka?" and get an answer via graph traversal, not keyword guessing.

**Decay** — Core memories live forever. Ephemeral notes fade in hours. Working context lasts about a week. Memories that get used stay alive; unused ones sink.

**Trust** — Mark memories as helpful or not. Bad info gets demoted 2x faster than good info gets promoted. Over time, your memory self-cleans.

## Quickstart

### Prerequisites

- Python 3.11+
- PostgreSQL 14+ with [pgvector](https://github.com/pgvector/pgvector)
- Redis 7+

### Install and initialize

```bash
pip install synapto
createdb synapto && psql -d synapto -c "CREATE EXTENSION vector;"
synapto init            # or: synapto init --interactive
```

### Connect to your agent

The recommended way is `uvx` with `--refresh` — every restart pulls the latest version from PyPI, no manual upgrades:

**Claude Code** (`~/.claude/.mcp.json`):

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

**Cursor** (`.cursor/mcp.json`):

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

> **Why `--refresh`?** Without it, `uvx` reuses the cached environment across restarts, so a new Synapto release on PyPI will not be picked up until the cache expires or you run `uv cache clean synapto` manually. `--refresh` tells `uv` to re-resolve the package on every launch, adding 1–3 seconds to startup in exchange for "always on the latest version" — the right default for an alpha project that ships often. Drop the flag (or pin a version like `"synapto==0.2.0"`) if you want to freeze the version.

Restart your agent. Synapto tools appear automatically, and any future release will be live on the next restart.

## MCP Tools

| Tool | What it does |
|------|-------------|
| `remember` | Store a memory (entities and search vectors are created automatically) |
| `recall` | Search memories by meaning |
| `get_memory` | Fetch the complete content and metadata for one recalled memory |
| `get_memories` | Fetch complete content for multiple recalled memories |
| `relate` | Link two entities ("Hermes" --[produces]--> "agent.messages") |
| `forget` | Soft-delete a memory |
| `trust_feedback` | Mark a memory as helpful or unhelpful |
| `find_contradictions` | Find memory pairs that disagree |
| `graph_query` | Walk the knowledge graph (N-hop) |
| `list_entities` | Browse known entities |
| `memory_stats` | View counts and distribution |
| `maintain` | Run decay and cleanup |

## CLI

```bash
synapto serve                   # start MCP server
synapto search "kafka topics"   # search from terminal
synapto doctor                  # check postgres, redis, embeddings health
synapto stats                   # memory statistics
synapto migrate status          # show applied/pending migrations
synapto export -o backup.json   # export memories
synapto import MEMORY.md --format markdown  # migrate from flat files
```

## Depth Layers

| Layer | Half-life | Example |
|-------|-----------|---------|
| `core` | Forever | "Our API uses REST, never GraphQL" |
| `stable` | ~6 months | "Auth service is in Go, everything else is Python" |
| `working` | ~1 week | "Currently refactoring the payment module" |
| `ephemeral` | ~6 hours | "Debugging: the timeout was 30s, changed to 60s" |

## How it works under the hood

When you call `recall("kafka patterns")`, Synapto runs three searches in parallel and fuses the results:

1. **Vector similarity** (pgvector HNSW) — finds semantically close memories
2. **Full-text search** (tsvector + BM25) — finds keyword matches
3. **HRR compositional algebra** — detects if "kafka" plays a structural role in the memory, not just appears as a word

The scores are combined via [Reciprocal Rank Fusion](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf), then weighted by decay, trust, and depth layer.

HRR (Holographic Reduced Representations) also enables queries that no vector database can do:

- **`probe("kafka")`** — find memories where Kafka is structurally involved (not just mentioned)
- **`reason(["kafka", "hermes"])`** — find memories about both entities simultaneously (vector-space AND)
- **`contradict()`** — find memory pairs that share entities but say different things

More in [docs/hrr.md](docs/hrr.md).

## Configuration

Config file: `~/.synapto/config.toml`

```toml
[postgresql]
dsn = "postgresql://localhost/synapto"

[redis]
url = "redis://localhost:6379/0"

[embeddings]
provider = ""  # auto-select (sentence-transformers on CPU, openai if API key set)
model = ""

[defaults]
tenant = "default"

[decay]
ephemeral_max_age_hours = 24
purge_after_days = 30
```

All values can be overridden with environment variables: `SYNAPTO_PG_DSN`, `SYNAPTO_REDIS_URL`, `SYNAPTO_EMBEDDING_PROVIDER`, `SYNAPTO_DEFAULT_TENANT`.

## Using as a Python library

```python
from synapto.db.postgres import PostgresClient
from synapto.db.migrations import run_migrations, ensure_hnsw_index
from synapto.embeddings.registry import get_provider
from synapto.search.hybrid import hybrid_search

pg = PostgresClient("postgresql://localhost/synapto")
await pg.connect()
await run_migrations(pg)

provider = get_provider()
await ensure_hnsw_index(pg, provider.dimension)

results = await hybrid_search(pg, provider, "outbox pattern", tenant="myproject")
for r in results:
    print(f"[{r.depth_layer}] trust={r.trust_score:.2f} {r.content}")
```

## Documentation

| | |
|---|---|
| [HRR deep dive](docs/hrr.md) | Compositional algebra, probe, reason, contradict |
| [Trust scoring](docs/trust-scoring.md) | Feedback loop and contradiction workflow |
| [Migrations](docs/migrations.md) | Versioned SQL files with rollback |
| [Claude Code](docs/claude-code.md) | Setup and usage with Claude Code |
| [Cursor](docs/cursor.md) | Setup and usage with Cursor |
| [LangGraph](docs/langgraph.md) | Using Synapto as a LangGraph tool |
| [Agno](docs/agno.md) | Using Synapto with Agno agents |

## Development

```bash
git clone https://github.com/ramonlimaramos/synapto.git
cd synapto
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
synapto init
pytest                          # 83 tests
```

## License

MIT
