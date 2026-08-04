"""Tests for migration discovery as a packaged resource.

These are the regressions the published v0.5.0 wheel needed and did not have.
The suite ran from a source checkout, where the old ``cwd`` fallback happened to
find the repository's ``migrations/`` directory, so nothing here was exercised
against an installed distribution. Everything below runs without a database.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from synapto.db.migrations import (
    MIGRATIONS_PACKAGE,
    MigrationDiscoveryError,
    discover_migrations,
    get_migration_status,
    migrate_down,
    migrate_up,
    run_migrations,
)

# The exact bundle the v0.5 line ships. 005/006 belong to the unreleased v0.6
# work and must never appear on this branch.
EXPECTED = {
    "001_initial.sql": "17ea18399343f8db",
    "002_add_hrr.sql": "9ed15ff823555af5",
    "003_metrics_events.sql": "ba4391fa1fd67b6e",
    "004_add_memory_subtype.sql": "9ac7f71c0e8c0300",
}

MIGRATION_BODY = "-- migrate:up\nSELECT 1;\n-- migrate:down\nSELECT 2;\n"


class TestBundledDiscovery:
    def test_returns_the_expected_migrations_in_order(self):
        found = discover_migrations()

        assert [m.filename for m in found] == list(EXPECTED)

    def test_checksums_match_the_release_baseline(self):
        # the checksums are the migration identity in synapto_migrations, so a
        # byte change here would silently orphan every existing database
        found = {m.filename: m.checksum for m in discover_migrations()}

        assert found == EXPECTED

    def test_does_not_ship_v0_6_migrations(self):
        names = [m.filename for m in discover_migrations()]

        assert not [n for n in names if n.startswith(("005", "006"))]

    def test_versions_are_sequential(self):
        assert [m.version for m in discover_migrations()] == [1, 2, 3, 4]

    def test_migrations_package_is_importable(self):
        from importlib import resources

        assert resources.files(MIGRATIONS_PACKAGE).is_dir()


class TestDiscoveryIgnoresTheWorkingDirectory:
    def test_a_foreign_cwd_migration_is_never_read(self, tmp_path, monkeypatch):
        # the old implementation fell back to Path.cwd() / "migrations", so a
        # process could read SQL from whatever directory it happened to run in
        foreign = tmp_path / "migrations"
        foreign.mkdir()
        (foreign / "999_foreign.sql").write_text(MIGRATION_BODY)
        monkeypatch.chdir(tmp_path)

        names = [m.filename for m in discover_migrations()]

        assert names == list(EXPECTED)
        assert "999_foreign.sql" not in names

    def test_discovery_is_identical_from_an_unrelated_directory(self, tmp_path, monkeypatch):
        from_repo = [m.filename for m in discover_migrations()]
        monkeypatch.chdir(tmp_path)

        assert [m.filename for m in discover_migrations()] == from_repo


class TestExplicitSources:
    def test_a_filesystem_override_still_works(self, tmp_path):
        (tmp_path / "007_extra.sql").write_text(MIGRATION_BODY)

        found = discover_migrations(tmp_path)

        assert [m.filename for m in found] == ["007_extra.sql"]

    def test_non_sql_entries_are_ignored(self, tmp_path):
        (tmp_path / "010_real.sql").write_text(MIGRATION_BODY)
        (tmp_path / "README.md").write_text("not a migration")
        (tmp_path / "notes.txt").write_text("also not")

        assert [m.filename for m in discover_migrations(tmp_path)] == ["010_real.sql"]

    def test_a_zip_backed_traversable_works_without_materializing(self, tmp_path):
        # proves the implementation consumes Traversable rather than converting
        # to a filesystem path, which is what makes a zipped distribution work
        archive = tmp_path / "bundle.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("_migrations/001_zipped.sql", MIGRATION_BODY)

        source = zipfile.Path(archive, "_migrations/")
        found = discover_migrations(source)

        assert [m.filename for m in found] == ["001_zipped.sql"]


class TestDiscoveryFailsClosed:
    def test_a_missing_source_raises(self, tmp_path):
        with pytest.raises(MigrationDiscoveryError):
            discover_migrations(tmp_path / "does-not-exist")

    def test_a_file_instead_of_a_directory_raises(self, tmp_path):
        not_a_dir = tmp_path / "migrations.sql"
        not_a_dir.write_text(MIGRATION_BODY)

        with pytest.raises(MigrationDiscoveryError):
            discover_migrations(not_a_dir)

    def test_an_empty_source_raises_rather_than_returning_nothing(self, tmp_path):
        # returning [] is the exact failure this replaces: the caller could not
        # tell "no migrations" from "could not find them" and initialized nothing
        with pytest.raises(MigrationDiscoveryError, match="no .sql migrations"):
            discover_migrations(tmp_path)

    def test_a_malformed_filename_raises_instead_of_being_skipped(self, tmp_path):
        (tmp_path / "not_numbered.sql").write_text(MIGRATION_BODY)

        with pytest.raises(MigrationDiscoveryError, match="malformed"):
            discover_migrations(tmp_path)

    def test_one_malformed_migration_fails_the_whole_bundle(self, tmp_path):
        (tmp_path / "001_fine.sql").write_text(MIGRATION_BODY)
        (tmp_path / "bad_name.sql").write_text(MIGRATION_BODY)

        with pytest.raises(MigrationDiscoveryError):
            discover_migrations(tmp_path)

    def test_undecodable_content_raises(self, tmp_path):
        (tmp_path / "001_binary.sql").write_bytes(b"\xff\xfe\x00invalid utf-8")

        with pytest.raises(MigrationDiscoveryError, match="cannot read"):
            discover_migrations(tmp_path)


class _RefusingClient:
    """Fails the test if any database call is attempted."""

    def __init__(self):
        self.calls = []

    async def execute(self, *args, **kwargs):
        self.calls.append(args)
        raise AssertionError("the database was contacted before migrations were validated")

    async def execute_one(self, *args, **kwargs):
        return await self.execute(*args, **kwargs)

    def acquire(self):
        raise AssertionError("the database was contacted before migrations were validated")


class TestEntryPointsValidateBeforeTouchingTheDatabase:
    """A broken bundle must never leave a half-initialized schema behind."""

    @pytest.fixture
    def broken_source(self, tmp_path):
        return tmp_path / "missing"

    async def test_migrate_up(self, broken_source):
        client = _RefusingClient()

        with pytest.raises(MigrationDiscoveryError):
            await migrate_up(client, broken_source)

        assert client.calls == []

    async def test_migrate_down(self, broken_source):
        client = _RefusingClient()

        with pytest.raises(MigrationDiscoveryError):
            await migrate_down(client, target_version=0, migrations_dir=broken_source)

        assert client.calls == []

    async def test_get_migration_status(self, broken_source):
        client = _RefusingClient()

        with pytest.raises(MigrationDiscoveryError):
            await get_migration_status(client, broken_source)

        assert client.calls == []

    async def test_run_migrations_validates_before_the_legacy_bridge(self, broken_source):
        # the legacy bridge swallows exceptions and writes to the database, so
        # discovery cannot be left to fail inside it
        client = _RefusingClient()

        with pytest.raises(MigrationDiscoveryError):
            await run_migrations(client, broken_source)

        assert client.calls == []


class TestVerifierScript:
    """The artifact gate itself — it is the thing standing between us and a repeat."""

    @pytest.fixture
    def verifier(self):
        import importlib.util

        path = Path(__file__).resolve().parents[2] / "scripts" / "verify_wheel.py"
        spec = importlib.util.spec_from_file_location("verify_wheel", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_expected_bundle_matches_the_shipped_migrations(self, verifier):
        assert verifier.EXPECTED_MIGRATIONS == EXPECTED

    def test_archive_check_accepts_a_correct_wheel(self, verifier, tmp_path):
        wheel = tmp_path / "synapto-0.5.1-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as zf:
            zf.writestr("synapto/__init__.py", "")
            for name in EXPECTED:
                zf.writestr(f"synapto/_migrations/{name}", MIGRATION_BODY)

        verifier._check_archive(wheel)  # must not raise

    def test_archive_check_rejects_a_wheel_without_migrations(self, verifier, tmp_path):
        # this is precisely what every published 0.1.0-0.5.0 wheel looks like
        wheel = tmp_path / "synapto-0.5.1-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as zf:
            zf.writestr("synapto/__init__.py", "")

        with pytest.raises(verifier.VerificationError):
            verifier._check_archive(wheel)

    def test_archive_check_rejects_a_root_migrations_copy(self, verifier, tmp_path):
        wheel = tmp_path / "synapto-0.5.1-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as zf:
            for name in EXPECTED:
                zf.writestr(f"synapto/_migrations/{name}", MIGRATION_BODY)
            zf.writestr("migrations/001_initial.sql", MIGRATION_BODY)

        with pytest.raises(verifier.VerificationError, match="outside"):
            verifier._check_archive(wheel)

    def test_archive_check_rejects_v0_6_migrations(self, verifier, tmp_path):
        wheel = tmp_path / "synapto-0.5.1-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as zf:
            for name in EXPECTED:
                zf.writestr(f"synapto/_migrations/{name}", MIGRATION_BODY)
            zf.writestr("synapto/_migrations/005_add_memory_domain.sql", MIGRATION_BODY)

        with pytest.raises(verifier.VerificationError):
            verifier._check_archive(wheel)

    def test_probe_check_rejects_an_out_of_environment_install(self, verifier, tmp_path):
        report = {
            "synapto_file": "/usr/lib/python3/synapto/__init__.py",
            "migrations": [[name, checksum] for name, checksum in EXPECTED.items()],
        }

        with pytest.raises(verifier.VerificationError, match="outside"):
            verifier._check_probe(report, tmp_path)

    def test_probe_check_rejects_a_foreign_migration(self, verifier, tmp_path):
        installed = tmp_path / "venv" / "lib" / "synapto" / "__init__.py"
        installed.parent.mkdir(parents=True)
        installed.touch()
        report = {
            "synapto_file": str(installed),
            "migrations": [["999_foreign.sql", "deadbeefdeadbeef"]],
        }

        with pytest.raises(verifier.VerificationError):
            verifier._check_probe(report, tmp_path)

    def test_missing_wheel_is_reported(self, verifier, tmp_path):
        assert verifier.main(["verify_wheel.py", str(tmp_path / "absent.whl")]) == 1

    def test_wrong_argument_count_is_reported(self, verifier):
        assert verifier.main(["verify_wheel.py"]) == 2
