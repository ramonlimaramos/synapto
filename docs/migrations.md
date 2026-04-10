# Migrations

Synapto uses versioned SQL files for schema management. Each migration has `up` and `down` sections, tracked with SHA-256 checksums.

## How It Works

Migrations live in `migrations/` as numbered SQL files:

```
migrations/
  001_initial.sql       # core schema (memories, entities, relations)
  002_add_hrr.sql       # HRR vectors, trust scoring, memory banks
```

Each file follows this format:

```sql
-- migrate:up
CREATE TABLE example (id SERIAL PRIMARY KEY);

-- migrate:down
DROP TABLE example;
```

Applied migrations are tracked in `synapto_migrations` (filename + checksum).

## CLI Commands

```bash
synapto migrate status          # show applied vs pending migrations
synapto migrate up              # apply all pending
synapto migrate up --to 1       # apply up to version 1 only
synapto migrate down --to 1     # rollback everything after version 1
```

## How Init Uses Migrations

`synapto init` calls the migration runner automatically. It also detects databases created with the old `synapto_schema` table and bridges them to the new system — migration 001 is marked as applied without re-running.

## Writing a New Migration

1. Create `migrations/003_your_change.sql`
2. Add `-- migrate:up` and `-- migrate:down` sections
3. Run `synapto migrate up`

The runner discovers files by glob, parses the sections, and applies them in version order. Checksums detect if a migration file was modified after being applied.

## Programmatic Usage

```python
from synapto.db.migrations import run_migrations, migrate_up, migrate_down, get_migration_status
from synapto.db.postgres import PostgresClient

pg = PostgresClient("postgresql://localhost/synapto")
await pg.connect()

# apply all pending
await run_migrations(pg)

# check status
status = await get_migration_status(pg)
for s in status:
    print(f"{s['filename']}: {s['status']}")

# rollback to version 1
await migrate_down(pg, target_version=1)
```
