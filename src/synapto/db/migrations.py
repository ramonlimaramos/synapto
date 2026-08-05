"""Versioned SQL migration runner for Synapto.

Uses the Iterator pattern to discover and apply numbered SQL files bundled as
package resources in ``synapto._migrations``. Each file contains
``-- migrate:up`` and ``-- migrate:down`` sections. Applied migrations are
tracked in a ``synapto_migrations`` table with SHA-256 checksums for tamper
detection.

Migration files must be named ``NNN_description.sql`` (e.g., ``001_initial.sql``).

Discovery reads the resource through :class:`importlib.resources.abc.Traversable`
rather than converting it to a filesystem path. The previous implementation
walked ``resources.files("synapto")`` up two parents to a presumed source
layout and fell back to ``Path.cwd() / "migrations"``. In an installed
distribution neither exists, so discovery returned an empty list and logged a
warning while the caller happily initialized nothing — and the cwd fallback
could also read unrelated SQL from whatever directory the process ran in.
Discovery now fails closed instead.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # keeps the module importable without third-party dependencies,
    # which is what lets the packaged-artifact verifier check a --no-deps install
    from synapto.db.postgres import PostgresClient

logger = logging.getLogger("synapto.db.migrations")

MIGRATIONS_PACKAGE = "synapto._migrations"

# NNN_description.sql — exactly three digits, so ordering is lexicographic and
# numeric at once, and the description cannot smuggle path separators.
MIGRATION_FILENAME = re.compile(r"(?P<version>[0-9]{3})_(?P<description>[A-Za-z0-9][A-Za-z0-9._-]*)\.sql")

UP_MARKER = "-- migrate:up"
DOWN_MARKER = "-- migrate:down"


class MigrationDiscoveryError(RuntimeError):
    """Raised when the migration bundle is missing, unreadable, or malformed.

    Always raised rather than warned: a caller that proceeds without migrations
    silently reports success against an uninitialized schema, which is the
    failure mode this replaces.
    """


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


def _split_sections(filename: str, content: str) -> tuple[str, str]:
    """Split a migration body into its up and down sections.

    Every structural rule is enforced rather than tolerated. The previous parser
    accepted a body with no markers at all and produced two empty sections,
    which the runner then recorded as a successfully applied migration — a
    malformed file became a silent no-op in the tracking table.
    """
    up_lines: list[str] = []
    down_lines: list[str] = []
    # the order of the markers actually recognized, not of text that merely
    # mentions them: a substring search saw marker words inside ordinary
    # comments, rejecting valid files and accepting reversed ones
    encountered: list[str] = []
    section: list[str] | None = None

    for line in content.split("\n"):
        marker = line.strip().lower()
        if marker == UP_MARKER:
            encountered.append(UP_MARKER)
            section = up_lines
            continue
        if marker == DOWN_MARKER:
            encountered.append(DOWN_MARKER)
            section = down_lines
            continue
        if section is not None:
            section.append(line)

    if encountered != [UP_MARKER, DOWN_MARKER]:
        raise MigrationDiscoveryError(
            f"migration {filename!r} must contain exactly one {UP_MARKER!r} followed by exactly one "
            f"{DOWN_MARKER!r} (found {encountered or 'none'})"
        )

    up_sql = "\n".join(up_lines).strip()
    down_sql = "\n".join(down_lines).strip()
    if not up_sql:
        raise MigrationDiscoveryError(f"migration {filename!r} has an empty up section")
    if not down_sql:
        raise MigrationDiscoveryError(f"migration {filename!r} has an empty down section")

    return up_sql, down_sql


def _parse_migration_file(resource: Traversable) -> Migration:
    """Parse a migration SQL resource into up/down sections.

    Takes a Traversable rather than a Path so the same code reads a source
    checkout, an installed wheel, and a zip-backed distribution.

    Raises:
        MigrationDiscoveryError: the name does not match ``NNN_description.sql``,
            the resource cannot be read, or the body is structurally invalid.
    """
    filename = resource.name

    match = MIGRATION_FILENAME.fullmatch(filename)
    if match is None:
        raise MigrationDiscoveryError(
            f"migration {filename!r} is malformed: expected NNN_description.sql with exactly three digits"
        )
    version = int(match.group("version"))
    if version <= 0:
        raise MigrationDiscoveryError(f"migration {filename!r} must have a positive version, got {version}")

    try:
        content = resource.read_text(encoding="utf-8")
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise MigrationDiscoveryError(f"cannot read migration {filename!r}: {exc}") from exc

    up_sql, down_sql = _split_sections(filename, content)

    return Migration(
        version=version,
        filename=filename,
        up_sql=up_sql,
        down_sql=down_sql,
        checksum=_compute_checksum(content),
    )


def discover_migrations(migrations_dir: Traversable | None = None) -> list[Migration]:
    """Discover and parse the bundled migrations, sorted by version.

    Reads the ``synapto._migrations`` package resource by default. An explicit
    source is still accepted for tests and operators; ``pathlib.Path`` satisfies
    the Traversable surface, so existing callers keep working.

    Raises:
        MigrationDiscoveryError: the source is missing, is not a directory,
            contains no SQL migrations, or contains one that cannot be parsed.
            Never returns an empty list — a caller cannot tell "no migrations"
            from "could not find them", so this refuses to make that ambiguous.
    """
    source = migrations_dir
    if source is None:
        try:
            source = resources.files(MIGRATIONS_PACKAGE)
        except (ModuleNotFoundError, TypeError) as exc:
            raise MigrationDiscoveryError(
                f"migration bundle {MIGRATIONS_PACKAGE!r} is not importable — "
                "the distribution is missing its packaged migrations"
            ) from exc

    # is_dir() and is_file() sit inside the guard too: a zipfile.Path aimed at a
    # file raises ValueError("Can't listdir a file") rather than
    # NotADirectoryError, and that escaped as a raw exception before
    try:
        if not source.is_dir():
            raise MigrationDiscoveryError(f"migration source {source!r} is not a directory")
        entries = sorted(source.iterdir(), key=lambda entry: entry.name)
        sql_entries = [entry for entry in entries if entry.name.endswith(".sql") and entry.is_file()]
    except MigrationDiscoveryError:
        raise
    except (OSError, ValueError) as exc:
        raise MigrationDiscoveryError(f"migration source {source!r} is not a readable directory: {exc}") from exc

    migrations = [_parse_migration_file(entry) for entry in sql_entries]

    if not migrations:
        raise MigrationDiscoveryError(f"migration source {source!r} contains no .sql migrations")

    return sorted(migrations, key=lambda m: m.version)


async def _ensure_tracking_table(client: PostgresClient) -> None:
    await client.execute(TRACKING_TABLE_DDL)


async def get_applied_migrations(client: PostgresClient) -> dict[str, str]:
    """Return a mapping of filename → checksum for all applied migrations."""
    await _ensure_tracking_table(client)
    rows = await client.execute("SELECT filename, checksum FROM synapto_migrations ORDER BY filename;")
    return {row["filename"]: row["checksum"] for row in rows}


async def migrate_up(
    client: PostgresClient,
    migrations_dir: Traversable | None = None,
    target_version: int | None = None,
) -> list[str]:
    """Apply all pending migrations (or up to target_version).

    Returns list of applied migration filenames.
    """
    # discovery first: a missing or malformed bundle must fail before the
    # database is touched, so a broken install never half-initializes a schema
    all_migrations = discover_migrations(migrations_dir)

    return await _apply_migrations(client, all_migrations, target_version)


async def _apply_migrations(
    client: PostgresClient,
    all_migrations: list[Migration],
    target_version: int | None = None,
) -> list[str]:
    """Apply already-parsed migrations.

    Takes the parsed list rather than a source so a caller that has already
    discovered them does not enumerate and re-read the resource a second time.
    ``run_migrations`` used to discover, write to the database through the
    legacy bridge, and then discover again inside ``migrate_up`` — with a
    mutable source, the second read could fail after those writes had landed.
    """
    await _ensure_tracking_table(client)
    applied = await get_applied_migrations(client)

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
    migrations_dir: Traversable | None = None,
) -> list[str]:
    """Rollback migrations down to (but not including) target_version.

    Returns list of rolled-back migration filenames.
    """
    # discovery first: a missing or malformed bundle must fail before the
    # database is touched, so a broken install never half-initializes a schema
    all_migrations = discover_migrations(migrations_dir)

    await _ensure_tracking_table(client)
    applied = await get_applied_migrations(client)

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
    migrations_dir: Traversable | None = None,
) -> list[dict]:
    """Return status of all migrations: applied or pending."""
    # discovery first: a missing or malformed bundle must fail before the
    # database is touched, so a broken install never half-initializes a schema
    all_migrations = discover_migrations(migrations_dir)

    await _ensure_tracking_table(client)
    applied = await get_applied_migrations(client)

    status = []
    for m in all_migrations:
        is_applied = m.filename in applied
        checksum_match = applied.get(m.filename) == m.checksum if is_applied else None
        status.append(
            {
                "version": m.version,
                "filename": m.filename,
                "status": "applied" if is_applied else "pending",
                "checksum_ok": checksum_match,
            }
        )
    return status


# ---------------------------------------------------------------------------
# Backward compatibility: bridge from old synapto_schema table
# ---------------------------------------------------------------------------


async def _migrate_from_legacy_schema(client: PostgresClient) -> bool:
    """Detect old synapto_schema table and mark migration 001 as applied.

    Returns True if legacy migration was detected and bridged.
    """
    try:
        row = await client.execute_one("SELECT 1 FROM information_schema.tables WHERE table_name = 'synapto_schema';")
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


async def run_migrations(client: PostgresClient, migrations_dir: Traversable | None = None) -> None:
    """Apply all pending migrations. Handles legacy schema detection."""
    # discovered once, before the legacy bridge writes anything: the bridge
    # swallows exceptions, and re-reading afterwards would let a source that
    # changed underneath us fail with database writes already committed
    all_migrations = discover_migrations(migrations_dir)

    await _migrate_from_legacy_schema(client)
    applied = await _apply_migrations(client, all_migrations)
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
