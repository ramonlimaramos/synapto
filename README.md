# Synapto

[![CI](https://github.com/ramonlimaramos/synapto/actions/workflows/ci.yml/badge.svg)](https://github.com/ramonlimaramos/synapto/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![PyPI version](https://img.shields.io/pypi/v/synapto.svg)](https://pypi.org/project/synapto/)

**Your AI agent forgets everything between sessions. Synapto fixes that.**

Flat-file memory (`MEMORY.md`) doesn't scale — no search, no structure, no decay. Synapto gives any MCP-compatible agent a real memory: store once, recall by meaning, watch bad memories fade and good ones persist.

```bash
# remember — while working inside ~/src/acme/api
"acme/api publishes events through an outbox table, never directly from the request handler"

# recall — weeks later, different session, same repository
"How does acme/api publish events?"
→ [stable] acme/api publishes events through an outbox table ... (score=0.94, trust=0.65)
```

Works with Claude Code, Cursor, Windsurf, Codex, LangGraph, Agno, or any MCP client.
Each repository gets its own memory partition automatically: the tenant is derived from the
working directory's git remote, so `acme/api` never sees `acme/web`'s notes unless you ask.

## Cross-agent handoffs

Pass work between Codex, Claude Code, Cursor, and other agents in plain
language. Synapto stores the structured state under the hood, so the next agent
can continue from a memory ID instead of a long pasted brief.

```text
You → Codex: Plan this feature and leave a handoff for Claude to implement.
Codex → You: Handoff created for Claude: b0e1506e-d1b7-4bee-9223-4d0f8d18a1b2

You → Claude: Continue from Synapto handoff b0e1506e-d1b7-4bee-9223-4d0f8d18a1b2.
Claude → You: I read the handoff, fetched its context, and can continue.
```

| What you say | What Synapto does |
|---|---|
| "Codex, leave this for Claude." | Stores a `project` memory with `metadata.kind = "agent_handoff"`. |
| "Claude, continue from this handoff ID." | Fetches the full memory with `get_memory` and verifies the metadata. |
| "Any handoffs for me?" | Uses `recall` to find ranked candidates, then fetches the relevant packet. |
| "Mark it ready for review." | Appends a follow-up memory with the same `task_id`. |

See [Cross-agent handoffs](docs/handoffs.md) for the lifecycle, schema, and
Claude/Cursor recipes.

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

**Scopes** — A memory can declare where it applies: `repo:acme/api`, `language:python`, `skill:code-review`, `global:all`. `recall(scopes=[...])` returns only what applies to the context you are in, and an exact-key `metadata_filter` narrows further ("every finding with `failure_class = missing_docstring`", with a true total, not a page size).

**Provenance** — Every memory records who wrote it: `human`, `agent`, or `consolidation`. Recall can filter by origin, and `forget` refuses to delete a human-authored memory unless told explicitly.

**Graph** — Entities are auto-extracted and linked. Ask "what depends on Kafka?" and get an answer via graph traversal, not keyword guessing.

**Decay** — Core memories live forever. Ephemeral notes fade in hours. Working context lasts about a week. Memories that get used stay alive; unused ones sink.

**Trust** — Mark memories as helpful or not. Bad info gets demoted 2x faster than good info gets promoted. Over time, your memory self-cleans.

**Handoffs** — Tell one agent to leave work for another in natural language.
Synapto turns that into a structured handoff memory, and the receiver continues
with `get_memory`, `context_ids`, and follow-up updates.

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

**Claude Code** — register the server at user scope, so it is available in every project:

```bash
claude mcp add --scope user synapto \
  -e CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 \
  -- uvx --refresh synapto serve
```

That writes the following entry to `~/.claude.json` (the file Claude Code actually reads for user-scoped servers — `~/.claude/.mcp.json` is not consulted):

```json
{
  "mcpServers": {
    "synapto": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--refresh", "synapto", "serve"],
      "env": {
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"
      }
    }
  }
}
```

Set `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` for Claude Code so Synapto remains the single memory sink instead of duplicating new memories into Claude's flat-file auto-memory.

Claude Code starts the server in the session's working directory, which is what makes per-repository tenants work: open a session inside `~/src/acme/api` and memories land in tenant `acme/api`. If you run the server from a checkout instead of PyPI, use `uv run --project /path/to/synapto synapto serve` rather than `uv --directory ...` — `--directory` changes the working directory before the server starts, and every session would then derive the checkout's own tenant.

Restart Claude Code after changing the configuration so the MCP subprocess receives the new environment.

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

## Default Memory Routing

Synapto is designed to be the primary memory sink for MCP-compatible agents. Agents should call `recall` before non-trivial work and call `remember` when users provide durable context instead of writing new memory into flat files.

| User signal | Tool action | Recommended type/layer |
|-------------|-------------|------------------------|
| "always X", "never Y", "from now on" | Store as a rule | `feedback` / `core`, subtype `workflow` |
| "don't do X", "that's wrong" | Store as a correction | `feedback` / `core`, subtype `communication` or `workflow` |
| "we use X for Y", "our architecture is..." | Store as project context | `project` / `stable`, subtype `stable` |
| "this sprint", "current PR", "release plan" | Store as active work | `project` / `working`, subtype `working` |
| "tracked in Linear", "dashboard is..." | Store as external reference | `reference` / `stable`, subtype `external_system` |
| "I work on...", "my preference is..." | Store as user context | `user` / `stable`, subtype `preference` |

`subtype` is optional and free-form. Recommended values include `code_style`, `workflow`, `tooling`, `testing`, `security`, `communication`, `external_system`, `documentation`, `role`, `preference`, `skill`, and `constraint`.

## Tenants and scopes

A **tenant** is the partition a memory lives in; a **scope** is where inside that partition it applies. Tenants keep repositories apart, scopes keep a Python rule from firing in a TypeScript file.

### Tenants are derived, not typed

Every tool resolves the tenant in this order:

1. An explicit `tenant=` argument — validated, never repaired. `Acme/API`, `git@github.com:acme/api.git`, and `acme/api/` are all rejected with the canonical form named in the error.
2. The `origin` remote of the git repository containing the server's working directory, normalised to `owner/name` (`https://github.com/acme/api.git` → `acme/api`).
3. `[defaults] tenant` in the config file, or `SYNAPTO_DEFAULT_TENANT`.
4. `default`.

Leave step 3 unset unless you have a reason: `default` is then an honest bucket for "no repository here", and nothing written outside a checkout is silently attributed to one.

Older installs that stored memories under hand-typed spellings (`api`, `acme-api`, `acme/api`) can be collapsed onto the canonical tenant:

```bash
synapto maintain --merge-tenants --dry-run   # report the proposed mapping
synapto maintain --merge-tenants --apply     # move the memories, record the aliases
```

Each fold is recorded in `tenant_aliases` (one hop, never a chain), so the merge is auditable. Reads do not yet follow aliases: a client that still sends an old spelling gets an empty, honest partition rather than a silent redirect.

### Scopes are typed

A scope is `"<type>:<key>"`. Six types exist: `global`, `product`, `repo`, `language`, `skill`, `workflow`. `global:all` is the only `global` key, and it cannot be combined with other scopes on the same memory.

```text
remember("Run ruff before every commit", scopes=["repo:acme/api", "language:python"])
recall("pre-commit checks", scopes=["repo:acme/api", "language:python"])
```

A scoped `recall` returns a memory when it is `global:all`, or when every scope type the memory declares is satisfied by one of the query's scopes of that type — a memory tagged `repo:acme/api` and `language:python` needs both to match; one tagged only `language:python` needs just the language. Memories stored without scopes are not returned by a scoped recall: unscoped means "never governed", not "applies everywhere" — use `global:all` for that. A `recall` without `scopes` ignores the axis entirely. Unknown types and malformed keys are rejected, never guessed.

`domain=` still works on `remember` and `recall` but is deprecated: it is one free-form label, whereas scopes are typed and plural. New callers should use `scopes`.

## MCP Tools

| Tool | What it does |
|------|-------------|
| `remember` | Store a memory with optional `scopes`, `origin` and `metadata` (entities and search vectors are created automatically) |
| `recall` | Search memories by meaning; narrow by `scopes`, `metadata_filter`, `origin`, `depth_layer`, `subtype` |
| `ping` | Check MCP transport health without touching PostgreSQL, Redis, or embeddings |
| `get_memory` | Fetch the complete content and metadata for one recalled memory |
| `get_memories` | Fetch complete content for multiple recalled memories |
| `update_memory` | Replace, append, or patch fields (including `scopes`) on an existing memory |
| `relate` | Link two entities ("acme/api" --[publishes]--> "orders.created") |
| `forget` | Soft-delete a memory; human-authored memories require `allow_human=true` |
| `trust_feedback` | Mark a memory as helpful or unhelpful |
| `find_contradictions` | Find memory pairs that disagree |
| `graph_query` | Walk the knowledge graph (N-hop) |
| `list_entities_tool` | Browse known entities |
| `memory_stats` | View counts and distribution |
| `maintain` | Run decay and ephemeral cleanup |
| `agent_handoff_template` | Build the structured `remember` payload for a cross-agent handoff |
| `handoff_inbox_template` | Build the `recall` call that lists handoffs waiting for an agent |

### Tool Field Limits

Synapto validates known hard limits before hitting the database, so MCP clients
get actionable errors instead of raw Postgres exceptions.

| Field | Limit |
|------|-------|
| `content` | Text; no Synapto length limit |
| `summary` | Max 255 characters |
| `memory_type` | Max 20 characters |
| `subtype` | Optional free-form subcategory, max 50 characters |
| `domain` | Deprecated in favour of `scopes`; max 50 characters |
| `depth_layer` | Max 20 characters |
| `tenant` | Canonical `owner/name`, max 100 characters |
| `scopes` | Up to 20 unique `"<type>:<key>"` entries per memory or query |
| `origin` | One of `human`, `agent`, `consolidation` |
| `recall.metadata_filter` | A flat JSON object of scalars, up to 20 keys; nested values are rejected because containment would not mean equality |
| `get_memories.memory_ids` | Max 20 IDs per call |
| `recall.preview_chars` | Clamped to 0-1000 characters |

## CLI

```bash
synapto serve                   # start MCP server
synapto init                    # create tables, indexes and extensions
synapto doctor                  # check postgres, redis, embeddings health
synapto search "kafka topics"   # search from terminal
synapto stats                   # memory statistics
synapto migrate status          # show applied/pending migrations
synapto migrate up              # apply pending migrations (serve does this on start)
synapto migrate down --to 7     # roll back everything after version 7
synapto maintain --merge-tenants --dry-run   # propose a tenant merge; --apply performs it
synapto export -o backup.json   # export memories
synapto import MEMORY.md --format markdown  # migrate from flat files
synapto migrate-memories        # detect and import other agents' memory files
synapto configure-mcp --client cursor   # write the MCP entry for Cursor
```

`configure-mcp --client claude-code` currently writes to `~/.claude/.mcp.json`, which Claude Code does not read ([#99](https://github.com/ramonlimaramos/synapto/issues/99)); use `claude mcp add` as shown above until that is fixed.

Migrations ship inside the package and `synapto serve` applies any that are pending when it starts, so upgrading the package is enough to upgrade the schema.

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

The scores are combined via [Reciprocal Rank Fusion](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf), then multiplied by decay, trust, and a depth-layer boost (`core` 1.5, `stable` 1.2, `working` 1.0, `ephemeral` 0.5). Filters — tenant, scopes, metadata, origin, layer, subtype — are applied inside the SQL before ranking, so a filtered recall is a smaller search, not a trimmed page.

HRR (Holographic Reduced Representations) also enables queries that no vector database can do:

- **`probe("kafka")`** — find memories where Kafka is structurally involved (not just mentioned)
- **`reason(["kafka", "acme/api"])`** — find memories about both entities simultaneously (vector-space AND)
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
provider = ""  # auto-select (sentence-transformers locally, openai if API key set)
model = ""
device = ""  # optional sentence-transformers device override, e.g. "cpu"

[defaults]
# tenant = "acme/api"   # fallback used only outside a git checkout; see "Tenants and scopes"

[decay]
ephemeral_max_age_hours = 24
purge_after_days = 30
```

All values can be overridden with environment variables: `SYNAPTO_PG_DSN`, `SYNAPTO_REDIS_URL`, `SYNAPTO_EMBEDDING_PROVIDER`, `SYNAPTO_EMBEDDING_MODEL`, `SYNAPTO_EMBEDDING_DEVICE`, `SYNAPTO_DEFAULT_TENANT`.

`SYNAPTO_DEFAULT_TENANT` (and `[defaults] tenant`) is a fallback, not a pin: it is consulted only when the working directory has no usable git remote. Set it when the server runs somewhere that is not a checkout — a container, a CI job, a shared shell — and you want those writes to land in a named partition. Inside repositories the derived tenant wins, and an unset fallback resolves to `default`. Like an explicit `tenant=` argument, a non-canonical value here is rejected rather than repaired, and the error names the config source instead of blaming the caller.

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
| [Cross-agent handoffs](docs/handoffs.md) | Coordinate planning, implementation, and review across agents |
| [Migrations](docs/migrations.md) | Versioned SQL files with rollback |
| [Claude Code](docs/claude-code.md) | Setup and usage with Claude Code |
| [Cursor](docs/cursor.md) | Setup and usage with Cursor |
| [LangGraph](docs/langgraph.md) | Using Synapto as a LangGraph tool |
| [Agno](docs/agno.md) | Using Synapto with Agno agents |

## Development

```bash
git clone https://github.com/ramonlimaramos/synapto.git
cd synapto
uv sync --extra dev            # or: python -m venv .venv && pip install -e ".[dev]"
uv run synapto init
uv run ruff check src/ tests/ scripts/
```

CI runs `ruff check`, `bandit`, `pip-audit`, a wheel build verified by `scripts/verify_wheel.py`, and the test suite on Python 3.11, 3.12 and 3.13.

Two conventions are enforced by tests rather than review:

- **SQL lives in `synapto/sql/`.** Every statement is a static constant with `%(name)s` parameters, one module per owner; Python selects statements, it never composes them. `tests/unit/test_sql_lives_in_the_sql_package.py` walks the AST of both sides.
- **Migrations are inventoried.** A new file under `synapto/_migrations/` must be added to `EXPECTED` in `tests/unit/test_migration_resources.py` and `EXPECTED_MIGRATIONS` in `scripts/verify_wheel.py` in the same commit, and a migration is never renumbered once merged. See [docs/migrations.md](docs/migrations.md).

### Running the tests

The PostgreSQL-backed tests are **destructive** — they roll migrations down (dropping and recreating columns) and truncate tables. They therefore refuse to run against anything but a disposable database, and they never read your production `SYNAPTO_PG_DSN`.

Create a throwaway database once:

```bash
createdb synapto_test
psql -d synapto_test -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_trgm;"
```

Then point `SYNAPTO_TEST_PG_DSN` at it:

```bash
SYNAPTO_TEST_PG_DSN=postgresql://localhost/synapto_test uv run pytest
```

Two fail-closed rules protect your real data:

- **No DSN, no connection.** With `SYNAPTO_TEST_PG_DSN` unset, the database-backed tests are skipped — never silently pointed at a default. `pytest` alone still runs every test that does not need PostgreSQL.
- **The database must be named `*_test`.** The suite asks the live connection for `current_database()` and aborts before any destructive setup if the name does not end in `_test`. Parsing the DSN is not enough: a DSN can omit the database name, and service files can redirect it.

## License

MIT
