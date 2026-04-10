"""Versioned SQL migration runner for Synapto.

Uses the Iterator pattern to discover and apply numbered SQL files from the
migrations/ directory. Each file contains ``-- migrate:up`` and ``-- migrate:down``
sections. Applied migrations are tracked in a ``synapto_migrations`` table with
SHA-256 checksums for tamper detection.

Migration files must be named ``NNN_description.sql`` (e.g., ``001_initial.sql``).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from synapto.db.postgres import PostgresClient

logger = logging.getLogger("synapto.db.migrations")

TRACKING_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS synapto_migrations (
    id SERIAL PRIMARY KEY,
    filename VARCHAR(255) NOT NULL UNIQUE,
    checksum VARCHAR(64) NOT NULL,
    applied_at TIMESTAMPTZ DEFAULT now()
);
"""


@dataclass(frozen=True)
class Migration:
    """A single versioned migration parsed from a SQL file."""

    version: int
    filename: str
    up_sql: str
    down_sql: str
    checksum: str


def _compute_checksum(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _parse_migration_file(path: Path) -> Migration:
    """Parse a migration SQL file into up/down sections.

    Expected format:
        -- migrate:up
        <SQL statements>
        -- migrate:down
        <SQL statements>
    """
    content = path.read_text()
    filename = path.name

    version_str = filename.split("_", 1)[0]
    version = int(version_str)

    up_sql = ""
    down_sql = ""
    current_section = None

    for line in content.split("\n"):
        stripped = line.strip().lower()
        if stripped == "-- migrate:up":
            current_section = "up"
            continue
        elif stripped == "-- migrate:down":
            current_section = "down"
            continue

        if current_section == "up":
            up_sql += line + "\n"
        elif current_section == "down":
            down_sql += line + "\n"

    return Migration(
        version=version,
        filename=filename,
        up_sql=up_sql.strip(),
        down_sql=down_sql.strip(),
        checksum=_compute_checksum(content),
    )


def discover_migrations(migrations_dir: Path | None = None) -> list[Migration]:
    """Discover and parse all migration SQL files, sorted by version."""
    if migrations_dir is None:
        # default: project root / migrations
        pkg = resources.files("synapto")
        migrations_dir = Path(str(pkg)).parent.parent / "migrations"
        if not migrations_dir.is_dir():
            # fallback: relative to cwd
            migrations_dir = Path.cwd() / "migrations"

    if not migrations_dir.is_dir():
        logger.warning("migrations directory not found: %s", migrations_dir)
        return []

    files = sorted(migrations_dir.glob("*.sql"))
    migrations = []
    for f in files:
        try:
            m = _parse_migration_file(f)
            migrations.append(m)
        except (ValueError, IndexError) as e:
            logger.warning("skipping invalid migration file %s: %s", f.name, e)

    return sorted(migrations, key=lambda m: m.version)


async def _ensure_tracking_table(client: PostgresClient) -> None:
    await client.execute(TRACKING_TABLE_DDL)


async def get_applied_migrations(client: PostgresClient) -> dict[str, str]:
    """Return a mapping of filename → checksum for all applied migrations."""
    await _ensure_tracking_table(client)
    rows = await client.execute(
        "SELECT filename, checksum FROM synapto_migrations ORDER BY filename;"
    )
    return {row["filename"]: row["checksum"] for row in rows}


async def migrate_up(
    client: PostgresClient,
    migrations_dir: Path | None = None,
    target_version: int | None = None,
) -> list[str]:
    """Apply all pending migrations (or up to target_version).

    Returns list of applied migration filenames.
    """
    await _ensure_tracking_table(client)
    applied = await get_applied_migrations(client)
    all_migrations = discover_migrations(migrations_dir)

    applied_files = []
    for m in all_migrations:
        if target_version is not None and m.version > target_version:
            break
        if m.filename in applied:
            continue

        logger.info("applying migration: %s", m.filename)
        async with client.acquire() as conn:
            await conn.execute(m.up_sql)
            await conn.execute(
                "INSERT INTO synapto_migrations (filename, checksum) VALUES (%s, %s) "
                "ON CONFLICT (filename) DO NOTHING;",
                (m.filename, m.checksum),
            )
        applied_files.append(m.filename)
        logger.info("migration applied: %s", m.filename)

    return applied_files


async def migrate_down(
    client: PostgresClient,
    target_version: int = 0,
    migrations_dir: Path | None = None,
) -> list[str]:
    """Rollback migrations down to (but not including) target_version.

    Returns list of rolled-back migration filenames.
    """
    await _ensure_tracking_table(client)
    applied = await get_applied_migrations(client)
    all_migrations = discover_migrations(migrations_dir)

    # rollback in reverse order
    rolled_back = []
    for m in reversed(all_migrations):
        if m.version <= target_version:
            break
        if m.filename not in applied:
            continue

        logger.info("rolling back migration: %s", m.filename)
        async with client.acquire() as conn:
            await conn.execute(m.down_sql)
            await conn.execute(
                "DELETE FROM synapto_migrations WHERE filename = %s;",
                (m.filename,),
            )
        rolled_back.append(m.filename)
        logger.info("migration rolled back: %s", m.filename)

    return rolled_back


async def get_migration_status(
    client: PostgresClient,
    migrations_dir: Path | None = None,
) -> list[dict]:
    """Return status of all migrations: applied or pending."""
    await _ensure_tracking_table(client)
    applied = await get_applied_migrations(client)
    all_migrations = discover_migrations(migrations_dir)

    status = []
    for m in all_migrations:
        is_applied = m.filename in applied
        checksum_match = applied.get(m.filename) == m.checksum if is_applied else None
        status.append({
            "version": m.version,
            "filename": m.filename,
            "status": "applied" if is_applied else "pending",
            "checksum_ok": checksum_match,
        })
    return status


# ---------------------------------------------------------------------------
# Backward compatibility: bridge from old synapto_schema table
# ---------------------------------------------------------------------------

async def _migrate_from_legacy_schema(client: PostgresClient) -> bool:
    """Detect old synapto_schema table and mark migration 001 as applied.

    Returns True if legacy migration was detected and bridged.
    """
    try:
        row = await client.execute_one(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'synapto_schema';"
        )
        if not row:
            return False

        # old system exists — mark 001 as already applied
        await _ensure_tracking_table(client)
        applied = await get_applied_migrations(client)
        if "001_initial.sql" not in applied:
            await client.execute(
                "INSERT INTO synapto_migrations (filename, checksum) VALUES (%s, %s) "
                "ON CONFLICT (filename) DO NOTHING;",
                ("001_initial.sql", "legacy"),
            )
            logger.info("legacy synapto_schema detected — marked 001_initial.sql as applied")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Convenience wrappers (used by init and server startup)
# ---------------------------------------------------------------------------

async def run_migrations(client: PostgresClient, migrations_dir: Path | None = None) -> None:
    """Apply all pending migrations. Handles legacy schema detection."""
    await _migrate_from_legacy_schema(client)
    applied = await migrate_up(client, migrations_dir)
    if applied:
        logger.info("applied %d migration(s): %s", len(applied), ", ".join(applied))
    else:
        logger.info("all migrations up to date")


async def get_schema_version(client: PostgresClient) -> int | None:
    """Return the highest applied migration version, or None if not initialized."""
    try:
        await _ensure_tracking_table(client)
        applied = await get_applied_migrations(client)
        if not applied:
            return None
        versions = []
        for filename in applied:
            try:
                v = int(filename.split("_", 1)[0])
                versions.append(v)
            except (ValueError, IndexError):
                pass
        return max(versions) if versions else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# HNSW index management (unchanged — dimension-dependent, not migratable)
# ---------------------------------------------------------------------------

HNSW_INDEX_TEMPLATE = """
    CREATE INDEX IF NOT EXISTS idx_{table}_embedding_{dim}
    ON {table} USING hnsw ((embedding::vector({dim})) vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
"""


async def ensure_hnsw_index(client: PostgresClient, dim: int) -> None:
    """Create HNSW indexes for a specific embedding dimension if they don't exist."""
    for table in ("memories", "entities"):
        sql = HNSW_INDEX_TEMPLATE.format(table=table, dim=dim)
        await client.execute(sql)
    logger.info("HNSW indexes ensured for dim=%d", dim)
