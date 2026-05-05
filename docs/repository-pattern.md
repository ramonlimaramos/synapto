# Repository Pattern

Synapto uses the **Repository Pattern** (GoF) to isolate all SQL from business logic. No raw SQL exists outside `src/synapto/repositories/` — every database operation goes through a repository class with named methods and SQL constants.

## Why Not an ORM?

Synapto deliberately avoids ORMs (SQLAlchemy, Django ORM, Tortoise) for several reasons:

- **pgvector operations** (`<=>` distance, `::vector(N)` casts) don't map cleanly to ORM abstractions
- **Recursive CTEs** for graph traversal are complex SQL that ORMs make harder, not easier
- **HRR bytea vectors** require raw binary handling that ORMs add unnecessary layers to
- **Performance control** — we need precise control over connection pooling, query plans, and batch operations
- **psycopg3 async** gives us everything we need without the abstraction tax

The Repository Pattern gives us the **organization benefits** of an ORM (no SQL scattered in business logic) without the **abstraction costs** (magic queries, N+1 surprises, migration generators).

## Architecture

```
src/synapto/
  repositories/          # all SQL lives here
    memories.py          # CRUD, decay, trust, HRR vectors, stats
    entities.py          # upsert, list, memory linking, counts
    relations.py         # upsert, direction queries, counts
    banks.py             # HRR memory bank management

  server.py              # MCP tools — calls repo methods, zero SQL
  graph/entities.py      # entity logic — delegates SQL to EntityRepository
  graph/relations.py     # relation logic — delegates SQL to RelationRepository
  search/hybrid.py       # search engine — delegates access tracking to MemoryRepository
  decay/maintenance.py   # decay scoring — delegates batch updates to MemoryRepository
  hrr/banks.py           # HRR banks — delegates storage to BankRepository + MemoryRepository
```

## How It Works

Each repository follows the same structure:

```python
# 1. SQL as named constants at the top
_INSERT = """
    INSERT INTO memories (content, embedding, ...)
    VALUES (%(content)s, %(emb)s, ...)
    RETURNING id;
"""

_SOFT_DELETE = """
    UPDATE memories SET deleted_at = now()
    WHERE id = %s AND deleted_at IS NULL
    RETURNING id;
"""

# 2. Repository class wraps PostgresClient
class MemoryRepository:
    def __init__(self, client: PostgresClient) -> None:
        self._db = client

    async def create(self, content: str, embedding: list[float], ...) -> UUID:
        row = await self._db.execute_one(_INSERT, {...})
        return row["id"]

    async def soft_delete(self, memory_id: str) -> list[dict]:
        return await self._db.execute(_SOFT_DELETE, (memory_id,))
```

Consumers instantiate a repository and call methods:

```python
# server.py — before (SQL inline)
rows = await pg.execute(
    "UPDATE memories SET deleted_at = now() WHERE id = %s AND deleted_at IS NULL RETURNING id;",
    (memory_id,),
)

# server.py — after (repository)
repo = MemoryRepository(pg)
rows = await repo.soft_delete(memory_id)
```

## Repository Reference

### MemoryRepository

| Method | Description |
|--------|-------------|
| `create()` | Insert a new memory with embedding |
| `get_by_id()` | Fetch a complete active memory by ID |
| `get_by_ids()` | Fetch multiple active memories by ID |
| `update_hrr()` | Store HRR vector for a memory |
| `soft_delete()` | Set deleted_at timestamp |
| `update_trust()` | Adjust trust score (asymmetric +0.05/-0.10) |
| `touch_accessed()` | Bump accessed_at and access_count for a list of IDs |
| `select_for_decay()` | Fetch memories needing decay score recalculation |
| `update_decay_scores()` | Batch update decay scores |
| `cleanup_ephemeral()` | Soft-delete stale ephemeral memories |
| `purge_deleted()` | Permanently remove old soft-deleted memories |
| `select_hrr_vectors()` | Fetch HRR vectors for bank rebuilding |
| `select_with_hrr()` | Fetch memories with HRR data for compositional retrieval |
| `count_by_type()` | Stats: memory count grouped by type |
| `count_by_depth()` | Stats: memory count grouped by depth layer |
| `count_by_tenant()` | Stats: memory count grouped by tenant |

### EntityRepository

| Method | Description |
|--------|-------------|
| `upsert()` | Create or update an entity (ON CONFLICT) |
| `get_by_name()` | Fetch entity by name + tenant |
| `list()` | List entities with optional type filter |
| `delete()` | Delete an entity by name |
| `link_memory()` | Create memory-entity association |
| `get_memory_entities()` | Get all entities linked to a memory |
| `count()` | Count entities, optionally filtered by tenant |

### RelationRepository

| Method | Description |
|--------|-------------|
| `upsert()` | Create or update a relation by entity IDs |
| `upsert_by_name()` | Create relation using entity names (joins internally) |
| `get_relations()` | Get relations for an entity (outgoing, incoming, or both) |
| `delete()` | Delete a relation by ID |
| `count()` | Count total relations |

### BankRepository

| Method | Description |
|--------|-------------|
| `upsert()` | Create or update a memory bank vector |
| `delete()` | Delete a bank by name |
| `get_vector()` | Retrieve a bank's bundled HRR vector |
| `list_tenant_types()` | List distinct memory types for a tenant |

## Scaling: Sharding and Multi-Replica

The Repository Pattern positions Synapto for horizontal scaling without changing business logic:

### Read Replicas

Since all SQL is centralized in repositories, routing read queries to replicas requires changes in **one place** — the `PostgresClient`:

```python
class PostgresClient:
    def __init__(self, write_dsn: str, read_dsn: str | None = None):
        self._write_pool = ...
        self._read_pool = ...  # points to replica

    async def execute(self, sql, params):      # writes go to primary
    async def execute_read(self, sql, params):  # reads go to replica
```

Repository methods that only read (`select_for_decay`, `count_by_type`, `list`, `get_by_name`) can switch to `execute_read()` — zero changes in server.py, graph/, or search/.

### Tenant-Based Sharding

Multi-tenant isolation is already built into every query (`WHERE tenant = %s`). Routing tenants to different database shards means:

1. `PostgresClient` accepts a tenant-to-shard mapping
2. Repositories pass tenant context through — they already receive it as a parameter
3. Business logic is completely unaware of sharding

```python
class ShardedPostgresClient:
    def __init__(self, shard_map: dict[str, str]):
        # {"tenant-a": "postgresql://shard1/...", "tenant-b": "postgresql://shard2/..."}
        self._pools = {tenant: AsyncConnectionPool(dsn) for tenant, dsn in shard_map.items()}
```

### Connection Pooling per Shard

Each shard gets its own connection pool with independent min/max sizes, preventing one tenant from starving others. The repository layer doesn't care — it calls `self._db.execute()` and the client routes to the right pool.

### Why This Matters

Without the Repository Pattern, scaling would require finding and modifying **38+ SQL calls scattered across 8 files**. With repositories, it's a change to **one class** (`PostgresClient`) and the repositories route automatically.
