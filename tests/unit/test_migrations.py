"""Unit tests for the versioned SQL migration runner."""

from __future__ import annotations

from textwrap import dedent

import pytest

from synapto.db.migrations import (
    _compute_checksum,
    _parse_migration_file,
    discover_migrations,
    get_applied_migrations,
    get_migration_status,
    get_schema_version,
    migrate_down,
    migrate_up,
    run_migrations,
)
from synapto.db.postgres import PostgresClient

DSN = "postgresql://localhost/synapto"


@pytest.fixture
async def pg():
    client = PostgresClient(DSN, min_size=1, max_size=2)
    await client.connect()
    yield client
    await client.close()


@pytest.fixture
def tmp_migrations(tmp_path):
    """Create a temporary migrations directory with test SQL files."""
    m1 = tmp_path / "001_create_foo.sql"
    m1.write_text(dedent("""\
        -- migrate:up
        CREATE TABLE IF NOT EXISTS test_foo (id SERIAL PRIMARY KEY, name TEXT);

        -- migrate:down
        DROP TABLE IF EXISTS test_foo;
    """))

    m2 = tmp_path / "002_add_bar.sql"
    m2.write_text(dedent("""\
        -- migrate:up
        ALTER TABLE test_foo ADD COLUMN IF NOT EXISTS bar TEXT;

        -- migrate:down
        ALTER TABLE test_foo DROP COLUMN IF EXISTS bar;
    """))

    return tmp_path


class TestMigrationParsing:
    def test_parse_migration_file(self, tmp_path):
        f = tmp_path / "001_test.sql"
        f.write_text("-- migrate:up\nCREATE TABLE t();\n-- migrate:down\nDROP TABLE t;")
        m = _parse_migration_file(f)
        assert m.version == 1
        assert m.filename == "001_test.sql"
        assert "CREATE TABLE" in m.up_sql
        assert "DROP TABLE" in m.down_sql
        assert len(m.checksum) == 16

    def test_discover_migrations(self, tmp_migrations):
        migrations = discover_migrations(tmp_migrations)
        assert len(migrations) == 2
        assert migrations[0].version == 1
        assert migrations[1].version == 2

    def test_discover_sorted_by_version(self, tmp_migrations):
        migrations = discover_migrations(tmp_migrations)
        versions = [m.version for m in migrations]
        assert versions == sorted(versions)

    def test_checksum_deterministic(self):
        assert _compute_checksum("hello") == _compute_checksum("hello")
        assert _compute_checksum("a") != _compute_checksum("b")


class TestMigrationRunner:
    async def test_migrate_up_applies_all(self, pg, tmp_migrations):
        applied = await migrate_up(pg, tmp_migrations)
        assert len(applied) == 2
        assert applied[0] == "001_create_foo.sql"
        assert applied[1] == "002_add_bar.sql"

        # cleanup
        await migrate_down(pg, target_version=0, migrations_dir=tmp_migrations)

    async def test_migrate_up_is_idempotent(self, pg, tmp_migrations):
        await migrate_up(pg, tmp_migrations)
        second_run = await migrate_up(pg, tmp_migrations)
        assert second_run == []

        await migrate_down(pg, target_version=0, migrations_dir=tmp_migrations)

    async def test_migrate_down_rollback(self, pg, tmp_migrations):
        await migrate_up(pg, tmp_migrations)
        rolled = await migrate_down(pg, target_version=1, migrations_dir=tmp_migrations)
        assert len(rolled) == 1
        assert "002_add_bar.sql" in rolled[0]

        # table should still exist but without bar column
        rows = await pg.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'test_foo' AND column_name = 'bar';"
        )
        assert len(rows) == 0

        await migrate_down(pg, target_version=0, migrations_dir=tmp_migrations)

    async def test_migrate_up_with_target(self, pg, tmp_migrations):
        applied = await migrate_up(pg, tmp_migrations, target_version=1)
        assert len(applied) == 1
        assert applied[0] == "001_create_foo.sql"

        await migrate_down(pg, target_version=0, migrations_dir=tmp_migrations)

    async def test_get_applied_migrations(self, pg, tmp_migrations):
        await migrate_up(pg, tmp_migrations)
        applied = await get_applied_migrations(pg)
        assert "001_create_foo.sql" in applied
        assert "002_add_bar.sql" in applied

        await migrate_down(pg, target_version=0, migrations_dir=tmp_migrations)

    async def test_get_migration_status(self, pg, tmp_migrations):
        await migrate_up(pg, tmp_migrations, target_version=1)
        status = await get_migration_status(pg, tmp_migrations)
        assert len(status) == 2
        assert status[0]["status"] == "applied"
        assert status[1]["status"] == "pending"

        await migrate_down(pg, target_version=0, migrations_dir=tmp_migrations)

    async def test_get_schema_version(self, pg, tmp_migrations):
        await migrate_up(pg, tmp_migrations)
        version = await get_schema_version(pg)
        assert version == 2

        await migrate_down(pg, target_version=0, migrations_dir=tmp_migrations)


class TestRunMigrationsCompat:
    """Test the run_migrations convenience wrapper used by init/server."""

    async def test_run_migrations_applies_project_migrations(self, pg):
        await run_migrations(pg)
        version = await get_schema_version(pg)
        assert version is not None and version >= 1

    async def test_run_migrations_is_idempotent(self, pg):
        await run_migrations(pg)
        await run_migrations(pg)
        version = await get_schema_version(pg)
        assert version is not None and version >= 1

    async def test_tables_exist_after_migration(self, pg):
        await run_migrations(pg)
        for table in ("memories", "entities", "relations", "memory_entities"):
            rows = await pg.execute(
                "SELECT tablename FROM pg_tables WHERE tablename = %s;", (table,)
            )
            assert len(rows) == 1, f"table {table} not found"
