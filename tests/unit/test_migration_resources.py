"""Tests for migration discovery as a packaged resource.

These are the regressions the published v0.5.0 wheel needed and did not have.
The suite ran from a source checkout, where the old ``cwd`` fallback happened to
find the repository's ``migrations/`` directory, so nothing here was exercised
against an installed distribution. Everything below runs without a database.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

from synapto.db.migrations import (
    MIGRATIONS_PACKAGE,
    MigrationDiscoveryError,
    _compute_checksum,
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


def _wheel_with_real_migrations(tmp_path, mutate: str | None = None) -> Path:
    """Build a wheel carrying the actual shipped migration bytes."""
    from importlib import resources

    wheel = tmp_path / "synapto-0.5.1-py3-none-any.whl"
    bundle = resources.files(MIGRATIONS_PACKAGE)
    with zipfile.ZipFile(wheel, "w") as zf:
        zf.writestr("synapto/__init__.py", "")
        for name in EXPECTED:
            body = (bundle / name).read_text(encoding="utf-8")
            if name == mutate:
                body += " "
            zf.writestr(f"synapto/_migrations/{name}", body)
    return wheel


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
        wheel = _wheel_with_real_migrations(tmp_path)

        verifier._check_archive(wheel)  # must not raise

    def test_archive_check_rejects_a_one_byte_mutation(self, verifier, tmp_path):
        # names alone would accept arbitrary SQL under the right filenames; the
        # checksum is the migration's identity in the tracking table
        wheel = _wheel_with_real_migrations(tmp_path, mutate="001_initial.sql")

        with pytest.raises(verifier.VerificationError, match="checksum"):
            verifier._check_archive(wheel)

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
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
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
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "synapto_file": str(installed),
            "migrations": [["999_foreign.sql", "deadbeefdeadbeef"]],
        }

        with pytest.raises(verifier.VerificationError):
            verifier._check_probe(report, tmp_path)

    def test_missing_wheel_is_reported(self, verifier, tmp_path):
        assert verifier.main(["verify_wheel.py", str(tmp_path / "absent.whl")]) == 1

    def test_wrong_argument_count_is_reported(self, verifier):
        assert verifier.main(["verify_wheel.py"]) == 2


class TestParserIsFailClosed:
    """A malformed migration must never become a recorded no-op.

    At the previously reviewed head a body with no markers parsed into two empty
    sections, and the runner happily inserted it into synapto_migrations as
    applied. These are the shapes that used to slip through.
    """

    def _write(self, tmp_path, name, body):
        (tmp_path / name).write_text(body)
        return tmp_path

    def test_a_body_with_no_markers_is_rejected(self, tmp_path):
        source = self._write(tmp_path, "001_plain.sql", "SELECT 1;\n")

        with pytest.raises(MigrationDiscoveryError, match="exactly one"):
            discover_migrations(source)

    def test_a_missing_down_marker_is_rejected(self, tmp_path):
        source = self._write(tmp_path, "001_up_only.sql", "-- migrate:up\nSELECT 1;\n")

        with pytest.raises(MigrationDiscoveryError, match="exactly one"):
            discover_migrations(source)

    def test_a_missing_up_marker_is_rejected(self, tmp_path):
        source = self._write(tmp_path, "001_down_only.sql", "-- migrate:down\nSELECT 1;\n")

        with pytest.raises(MigrationDiscoveryError, match="exactly one"):
            discover_migrations(source)

    def test_reversed_markers_are_rejected(self, tmp_path):
        source = self._write(tmp_path, "001_reversed.sql", "-- migrate:down\nSELECT 2;\n-- migrate:up\nSELECT 1;\n")

        with pytest.raises(MigrationDiscoveryError, match="followed by"):
            discover_migrations(source)

    def test_duplicate_markers_are_rejected(self, tmp_path):
        body = "-- migrate:up\nSELECT 1;\n-- migrate:up\nSELECT 3;\n-- migrate:down\nSELECT 2;\n"
        source = self._write(tmp_path, "001_dupe.sql", body)

        with pytest.raises(MigrationDiscoveryError, match="exactly one"):
            discover_migrations(source)

    def test_an_empty_up_section_is_rejected(self, tmp_path):
        source = self._write(tmp_path, "001_empty_up.sql", "-- migrate:up\n\n-- migrate:down\nSELECT 2;\n")

        with pytest.raises(MigrationDiscoveryError, match="empty up"):
            discover_migrations(source)

    def test_an_empty_down_section_is_rejected(self, tmp_path):
        source = self._write(tmp_path, "001_empty_down.sql", "-- migrate:up\nSELECT 1;\n-- migrate:down\n\n")

        with pytest.raises(MigrationDiscoveryError, match="empty down"):
            discover_migrations(source)

    @pytest.mark.parametrize("name", ["1_short.sql", "01_short.sql", "0001_long.sql", "000_zero.sql", "-01_neg.sql"])
    def test_non_three_digit_prefixes_are_rejected(self, tmp_path, name):
        source = self._write(tmp_path, name, MIGRATION_BODY)

        with pytest.raises(MigrationDiscoveryError):
            discover_migrations(source)

    def test_an_empty_description_is_rejected(self, tmp_path):
        source = self._write(tmp_path, "001_.sql", MIGRATION_BODY)

        with pytest.raises(MigrationDiscoveryError, match="malformed"):
            discover_migrations(source)

    def test_a_valid_body_keeps_its_sections_and_checksum(self, tmp_path):
        body = "-- migrate:up\nCREATE TABLE t();\n-- migrate:down\nDROP TABLE t;\n"
        source = self._write(tmp_path, "001_valid.sql", body)

        parsed = discover_migrations(source)[0]

        assert parsed.up_sql == "CREATE TABLE t();"
        assert parsed.down_sql == "DROP TABLE t;"
        assert parsed.checksum == _compute_checksum(body)

    def test_markers_are_matched_case_insensitively(self, tmp_path):
        source = self._write(tmp_path, "001_case.sql", "-- MIGRATE:UP\nSELECT 1;\n-- Migrate:Down\nSELECT 2;\n")

        assert discover_migrations(source)[0].up_sql == "SELECT 1;"


class _CountingSource:
    """Wraps a Traversable and counts how many times it is enumerated."""

    def __init__(self, inner):
        self._inner = inner
        self.iterdir_calls = 0

    @property
    def name(self):
        return self._inner.name

    def is_dir(self):
        return self._inner.is_dir()

    def is_file(self):
        return self._inner.is_file()

    def iterdir(self):
        self.iterdir_calls += 1
        return self._inner.iterdir()

    def read_text(self, encoding="utf-8"):
        return self._inner.read_text(encoding=encoding)


class _NullConnection:
    """Accepts the apply statements without a database."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *args, **kwargs):
        return None


class _RecordingClient:
    """Accepts database calls and records when they happened."""

    def __init__(self, source):
        self._source = source
        self.enumerations_at_first_call = None

    def _note(self):
        if self.enumerations_at_first_call is None:
            self.enumerations_at_first_call = self._source.iterdir_calls

    async def execute(self, *args, **kwargs):
        self._note()
        return []

    async def execute_one(self, *args, **kwargs):
        self._note()
        return None

    def acquire(self):
        self._note()
        return _NullConnection()


class TestSourceIsReadOnlyOnce:
    async def test_run_migrations_enumerates_once(self, tmp_path):
        # run_migrations used to discover, write through the legacy bridge, and
        # then discover again inside migrate_up — a source that changed in
        # between could fail with database writes already committed
        (tmp_path / "001_only.sql").write_text(MIGRATION_BODY)
        source = _CountingSource(tmp_path)
        client = _RecordingClient(source)

        await run_migrations(client, source)

        assert source.iterdir_calls == 1
        assert client.enumerations_at_first_call == 1

    async def test_migrate_up_enumerates_once(self, tmp_path):
        (tmp_path / "001_only.sql").write_text(MIGRATION_BODY)
        source = _CountingSource(tmp_path)

        await migrate_up(_RecordingClient(source), source)

        assert source.iterdir_calls == 1


class TestTraversableErrorsAreConverted:
    def test_a_zip_path_pointing_at_a_file_is_rejected(self, tmp_path):
        # zipfile.Path raises ValueError("Can't listdir a file"), which used to
        # escape as a raw exception rather than a MigrationDiscoveryError
        archive = tmp_path / "bundle.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("_migrations/001_zipped.sql", MIGRATION_BODY)

        with pytest.raises(MigrationDiscoveryError):
            discover_migrations(zipfile.Path(archive, "_migrations/001_zipped.sql"))

    def test_an_entry_that_fails_inspection_is_reported(self, tmp_path):
        class _ExplodingEntry:
            name = "001_boom.sql"

            def is_file(self):
                raise OSError("stat failed")

        class _Source:
            def is_dir(self):
                return True

            def iterdir(self):
                return [_ExplodingEntry()]

        with pytest.raises(MigrationDiscoveryError, match="not a readable directory"):
            discover_migrations(_Source())


class TestMarkerOrderUsesRealMarkers:
    """Order came from a substring search, which saw marker text in comments."""

    def test_a_preamble_mentioning_down_does_not_reject_a_valid_file(self, tmp_path):
        body = "-- preamble mentions -- migrate:down in prose\n-- migrate:up\nSELECT 1;\n-- migrate:down\nSELECT 2;\n"
        (tmp_path / "001_commented.sql").write_text(body)

        parsed = discover_migrations(tmp_path)[0]

        assert parsed.up_sql == "SELECT 1;"
        assert parsed.down_sql == "SELECT 2;"

    def test_a_preamble_mentioning_up_does_not_accept_reversed_markers(self, tmp_path):
        body = "-- preamble mentions -- migrate:up in prose\n-- migrate:down\nSELECT 2;\n-- migrate:up\nSELECT 1;\n"
        (tmp_path / "001_reversed_commented.sql").write_text(body)

        with pytest.raises(MigrationDiscoveryError):
            discover_migrations(tmp_path)

    def test_unicode_decimal_digits_do_not_satisfy_the_filename_shape(self, tmp_path):
        # \d matches Unicode decimals, so Arabic-Indic digits would have passed
        # for a name that is not the documented ASCII NNN shape
        (tmp_path / "١٢٣_unicode.sql").write_text(MIGRATION_BODY)

        with pytest.raises(MigrationDiscoveryError, match="malformed"):
            discover_migrations(tmp_path)


class TestVerifierRejectsInterpreterMismatch:
    def test_a_child_on_a_different_python_is_rejected(self, tmp_path):
        import importlib.util

        path = Path(__file__).resolve().parents[2] / "scripts" / "verify_wheel.py"
        spec = importlib.util.spec_from_file_location("verify_wheel_mismatch", path)
        verifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verifier)

        installed = tmp_path / "venv" / "lib" / "synapto" / "__init__.py"
        installed.parent.mkdir(parents=True)
        installed.touch()
        report = {
            "python": "2.7",  # never the parent
            "synapto_file": str(installed),
            "migrations": [[name, checksum] for name, checksum in EXPECTED.items()],
        }

        with pytest.raises(verifier.VerificationError, match="expected"):
            verifier._check_probe(report, tmp_path)


class _TrackingSource:
    """Counts every enumeration and every resource read."""

    def __init__(self, inner):
        self._inner = inner
        self.iterdir_calls = 0
        self.read_calls = 0

    @property
    def name(self):
        return self._inner.name

    def is_dir(self):
        return self._inner.is_dir()

    def is_file(self):
        return self._inner.is_file()

    def iterdir(self):
        self.iterdir_calls += 1
        return [_TrackedEntry(entry, self) for entry in self._inner.iterdir()]

    def read_text(self, encoding="utf-8"):
        self.read_calls += 1
        return self._inner.read_text(encoding=encoding)


class _TrackedEntry:
    def __init__(self, inner, tracker):
        self._inner = inner
        self._tracker = tracker

    @property
    def name(self):
        return self._inner.name

    def is_file(self):
        return self._inner.is_file()

    def read_text(self, encoding="utf-8"):
        self._tracker.read_calls += 1
        return self._inner.read_text(encoding=encoding)


class _SnapshottingClient:
    """Records the source's access counts at the moment of the first DB call."""

    def __init__(self, source):
        self._source = source
        self.snapshot = None

    def _note(self):
        if self.snapshot is None:
            self.snapshot = (self._source.iterdir_calls, self._source.read_calls)

    async def execute(self, *args, **kwargs):
        self._note()
        return []

    async def execute_one(self, *args, **kwargs):
        self._note()
        return None

    def acquire(self):
        self._note()
        return _NullConnection()


class TestResourceIsReadOnceBeforeAnyDatabaseCall:
    async def test_run_migrations_reads_each_migration_exactly_once(self, tmp_path):
        for index in (1, 2):
            (tmp_path / f"00{index}_only.sql").write_text(MIGRATION_BODY)
        source = _TrackingSource(tmp_path)
        client = _SnapshottingClient(source)

        await run_migrations(client, source)

        # one enumeration, one read per migration, and every one of them before
        # the first database call
        assert source.iterdir_calls == 1
        assert source.read_calls == 2
        assert client.snapshot == (1, 2)

    async def test_no_resource_access_happens_after_the_first_database_call(self, tmp_path):
        (tmp_path / "001_only.sql").write_text(MIGRATION_BODY)
        source = _TrackingSource(tmp_path)
        client = _SnapshottingClient(source)

        await run_migrations(client, source)

        assert (source.iterdir_calls, source.read_calls) == client.snapshot


class _LegacyDatabase:
    """A stateful fake standing in for a database created before the runner existed.

    Models only what the bridge and the runner touch: the presence of the old
    ``synapto_schema`` table, the tracking table's contents, and which up-SQL
    statements were executed. Everything is counted so the test can assert
    "exactly once" rather than "at least once".
    """

    LEGACY_PROBE = "information_schema.tables"

    def __init__(self, *, legacy: bool):
        self.legacy = legacy
        self.tracking: dict[str, str] = {}
        self.executed_up_sql: list[str] = []
        self.tracking_inserts: list[tuple[str, str]] = []

    async def execute_one(self, query, params=None):
        if self.LEGACY_PROBE in query:
            return {"exists": 1} if self.legacy else None
        return None

    async def execute(self, query, params=None):
        if "CREATE TABLE IF NOT EXISTS synapto_migrations" in query:
            return []
        if "SELECT filename, checksum FROM synapto_migrations" in query:
            return [{"filename": name, "checksum": checksum} for name, checksum in self.tracking.items()]
        if "INSERT INTO synapto_migrations" in query:
            self._record(params)
            return []
        return []

    def _record(self, params):
        filename, checksum = params
        self.tracking_inserts.append((filename, checksum))
        self.tracking.setdefault(filename, checksum)

    def acquire(self):
        return _LegacyConnection(self)


class _LegacyConnection:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, query, params=None):
        if "INSERT INTO synapto_migrations" in query:
            self._db._record(params)
        else:
            self._db.executed_up_sql.append(query)
        return None


class TestLegacySchemaBridge:
    """The compatibility path for databases predating the migration runner.

    Existing coverage only exercised a clean database, so the bridge itself —
    the branch that decides an old schema already contains migration 001 — was
    never asserted.
    """

    @pytest.fixture
    def bundle(self, tmp_path):
        # 001 must carry the real name the bridge marks, or the suppression the
        # bridge exists to perform would not be exercised at all
        names = {1: "001_initial.sql", 2: "002_step.sql", 3: "003_step.sql"}
        for index, name in names.items():
            (tmp_path / name).write_text(f"-- migrate:up\nSELECT {index};\n-- migrate:down\nSELECT -{index};\n")
        return tmp_path

    async def test_a_legacy_database_marks_001_once_without_running_it(self, bundle):
        db = _LegacyDatabase(legacy=True)

        await run_migrations(db, bundle)

        # 001 is recorded as already satisfied by the old schema...
        assert db.tracking["001_initial.sql"] == "legacy"
        assert [f for f, _ in db.tracking_inserts].count("001_initial.sql") == 1
        # ...and its up SQL is never executed, which is the whole point
        assert "SELECT 1;" not in db.executed_up_sql

    async def test_the_remaining_migrations_are_applied_exactly_once(self, bundle):
        db = _LegacyDatabase(legacy=True)

        await run_migrations(db, bundle)

        # 001 is skipped because the bridge already recorded it
        assert db.executed_up_sql == ["SELECT 2;", "SELECT 3;"]
        assert sorted(db.tracking) == ["001_initial.sql", "002_step.sql", "003_step.sql"]

    async def test_a_second_run_applies_and_records_nothing(self, bundle):
        db = _LegacyDatabase(legacy=True)
        await run_migrations(db, bundle)
        applied_first = list(db.executed_up_sql)
        inserts_first = list(db.tracking_inserts)

        await run_migrations(db, bundle)

        assert db.executed_up_sql == applied_first
        assert db.tracking_inserts == inserts_first

    async def test_a_clean_database_does_not_get_the_legacy_marker(self, bundle):
        db = _LegacyDatabase(legacy=False)

        await run_migrations(db, bundle)

        # no legacy marker, so 001 runs like any other migration
        assert db.tracking["001_initial.sql"] != "legacy"
        assert db.executed_up_sql == ["SELECT 1;", "SELECT 2;", "SELECT 3;"]


class TestVerifierHandlesBrokenArchives:
    """Corruption must be an actionable failure, never a traceback."""

    @pytest.fixture
    def verifier(self):
        import importlib.util

        path = Path(__file__).resolve().parents[2] / "scripts" / "verify_wheel.py"
        spec = importlib.util.spec_from_file_location("verify_wheel_broken", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_a_non_zip_file_is_reported_without_a_traceback(self, verifier, tmp_path):
        not_a_wheel = tmp_path / "synapto-0.5.1-py3-none-any.whl"
        not_a_wheel.write_text("[project]\nname = 'not a wheel'\n")

        with pytest.raises(verifier.VerificationError, match="cannot open"):
            verifier._check_archive(not_a_wheel)

        assert verifier.main(["verify_wheel.py", str(not_a_wheel)]) == 1

    def test_a_truncated_zip_is_reported(self, verifier, tmp_path):
        wheel = _wheel_with_real_migrations(tmp_path)
        truncated = tmp_path / "truncated.whl"
        truncated.write_bytes(wheel.read_bytes()[: len(wheel.read_bytes()) // 3])

        with pytest.raises(verifier.VerificationError):
            verifier._check_archive(truncated)

    def test_a_member_with_invalid_bytes_is_rejected(self, verifier, tmp_path):
        # distinct from the one-byte mutation case: this member is not even
        # valid UTF-8, which used to escape as UnicodeDecodeError
        wheel = tmp_path / "synapto-0.5.1-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as zf:
            for name in EXPECTED:
                zf.writestr(f"synapto/_migrations/{name}", b"\xff\xfe not utf-8")

        with pytest.raises(verifier.VerificationError, match="checksum"):
            verifier._check_archive(wheel)


class TestSdistIsGated:
    @pytest.fixture
    def verifier(self):
        import importlib.util

        path = Path(__file__).resolve().parents[2] / "scripts" / "verify_wheel.py"
        spec = importlib.util.spec_from_file_location("verify_wheel_sdist", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _sdist(self, tmp_path, *, names=None, mutate=None, root_copy=False):
        import io
        import tarfile
        from importlib import resources

        bundle = resources.files(MIGRATIONS_PACKAGE)
        path = tmp_path / "synapto-0.5.1.tar.gz"
        with tarfile.open(path, "w:gz") as tar:
            for name in names or EXPECTED:
                raw = (bundle / name).read_text(encoding="utf-8").encode()
                if name == mutate:
                    raw += b" "
                for member_name in [f"synapto-0.5.1/src/synapto/_migrations/{name}"] + (
                    [f"synapto-0.5.1/migrations/{name}"] if root_copy else []
                ):
                    info = tarfile.TarInfo(member_name)
                    info.size = len(raw)
                    tar.addfile(info, io.BytesIO(raw))
        return path

    def test_a_correct_sdist_passes(self, verifier, tmp_path):
        verifier._check_sdist(self._sdist(tmp_path))  # must not raise

    def test_a_mutated_sdist_member_is_rejected(self, verifier, tmp_path):
        with pytest.raises(verifier.VerificationError, match="expected"):
            verifier._check_sdist(self._sdist(tmp_path, mutate="001_initial.sql"))

    def test_a_root_migrations_copy_is_rejected(self, verifier, tmp_path):
        with pytest.raises(verifier.VerificationError, match="expected"):
            verifier._check_sdist(self._sdist(tmp_path, root_copy=True))

    def test_a_missing_migration_is_rejected(self, verifier, tmp_path):
        partial = {k: v for k, v in EXPECTED.items() if k != "004_add_memory_subtype.sql"}

        with pytest.raises(verifier.VerificationError, match="expected"):
            verifier._check_sdist(self._sdist(tmp_path, names=partial))

    def test_a_non_tar_file_is_reported(self, verifier, tmp_path):
        broken = tmp_path / "synapto-0.5.1.tar.gz"
        broken.write_text("not a tarball")

        with pytest.raises(verifier.VerificationError, match="gzip"):
            verifier._check_sdist(broken)


def _sdist_with(tmp_path, members):
    """Build a source distribution from explicit (path, bytes, kind) members."""
    import io
    import tarfile

    path = tmp_path / "synapto-0.5.1.tar.gz"
    path.unlink(missing_ok=True)
    with tarfile.open(path, "w:gz") as tar:
        for name, content, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = "../nowhere.sql"
                info.size = 0
                tar.addfile(info)
            elif kind == "hardlink":
                info.type = tarfile.LNKTYPE
                info.linkname = "synapto-0.5.1/src/synapto/_migrations/001_initial.sql"
                info.size = 0
                tar.addfile(info)
            else:
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
    return path


def _canonical_members():
    from importlib import resources

    bundle = resources.files(MIGRATIONS_PACKAGE)
    return [
        (f"synapto-0.5.1/src/synapto/_migrations/{name}", (bundle / name).read_text(encoding="utf-8").encode(), "file")
        for name in EXPECTED
    ]


class TestSdistGateIsExact:
    """Membership must be exact path equality, not containment.

    A substring test accepted all four files under a decoy directory, accepted a
    fifth duplicate beside the canonical four, and accepted `..` traversal.
    """

    @pytest.fixture
    def verifier(self):
        import importlib.util

        path = Path(__file__).resolve().parents[2] / "scripts" / "verify_wheel.py"
        spec = importlib.util.spec_from_file_location("verify_wheel_exact", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_canonical_sdist_passes(self, verifier, tmp_path):
        verifier._check_sdist(_sdist_with(tmp_path, _canonical_members()))  # must not raise

    def test_migrations_in_a_decoy_directory_are_rejected(self, verifier, tmp_path):
        members = [(name.replace("/src/", "/not-package/"), body, kind) for name, body, kind in _canonical_members()]

        with pytest.raises(verifier.VerificationError, match="expected"):
            verifier._check_sdist(_sdist_with(tmp_path, members))

    def test_a_duplicate_canonical_member_is_rejected(self, verifier, tmp_path):
        members = _canonical_members()
        members.append(("synapto-0.5.1/other/synapto/_migrations/001_initial.sql", members[0][1], "file"))

        with pytest.raises(verifier.VerificationError):
            verifier._check_sdist(_sdist_with(tmp_path, members))

    def test_a_decoy_with_conflicting_bytes_is_rejected(self, verifier, tmp_path):
        members = _canonical_members()
        members.append(("synapto-0.5.1/src/synapto/_migrations/001_initial.sql", b"different bytes", "file"))

        with pytest.raises(verifier.VerificationError):
            verifier._check_sdist(_sdist_with(tmp_path, members))

    def test_a_path_with_parent_traversal_is_rejected(self, verifier, tmp_path):
        members = [(name.replace("/src/", "/../evil/"), body, kind) for name, body, kind in _canonical_members()]

        with pytest.raises(verifier.VerificationError):
            verifier._check_sdist(_sdist_with(tmp_path, members))

    @pytest.mark.parametrize("kind", ["symlink", "hardlink"])
    def test_a_non_regular_member_is_rejected(self, verifier, tmp_path, kind):
        members = _canonical_members()[:3]
        members.append(("synapto-0.5.1/src/synapto/_migrations/004_add_memory_subtype.sql", b"", kind))

        with pytest.raises(verifier.VerificationError):
            verifier._check_sdist(_sdist_with(tmp_path, members))

    def test_a_v0_6_migration_is_rejected(self, verifier, tmp_path):
        members = _canonical_members()
        members.append(
            ("synapto-0.5.1/src/synapto/_migrations/005_add_memory_domain.sql", MIGRATION_BODY.encode(), "file")
        )

        with pytest.raises(verifier.VerificationError):
            verifier._check_sdist(_sdist_with(tmp_path, members))

    def test_several_top_level_roots_are_rejected(self, verifier, tmp_path):
        members = _canonical_members()
        members.append(("other-root/README.md", b"decoy", "file"))

        with pytest.raises(verifier.VerificationError, match="outside"):
            verifier._check_sdist(_sdist_with(tmp_path, members))

    def test_a_broken_sdist_returns_one_through_main_without_a_traceback(self, verifier, tmp_path, capsys):
        # a dangling link used to escape main as a raw KeyError
        wheel = _wheel_with_real_migrations(tmp_path)
        members = _canonical_members()[:3]
        members.append(("synapto-0.5.1/src/synapto/_migrations/004_add_memory_subtype.sql", b"", "symlink"))
        sdist = _sdist_with(tmp_path, members)

        assert verifier.main(["verify_wheel.py", str(wheel), str(sdist)]) == 1

        captured = capsys.readouterr()
        assert "FAILED" in captured.err
        assert "Traceback" not in captured.err


class TestWheelMemberMetadata:
    @pytest.fixture
    def verifier(self):
        import importlib.util

        path = Path(__file__).resolve().parents[2] / "scripts" / "verify_wheel.py"
        spec = importlib.util.spec_from_file_location("verify_wheel_meta", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _wheel(self, tmp_path, *, link_member=None):
        import stat as stat_module
        from importlib import resources

        bundle = resources.files(MIGRATIONS_PACKAGE)
        wheel = tmp_path / "synapto-0.5.1-py3-none-any.whl"
        wheel.unlink(missing_ok=True)
        with zipfile.ZipFile(wheel, "w") as zf:
            for name in EXPECTED:
                info = zipfile.ZipInfo(f"synapto/_migrations/{name}")
                mode = stat_module.S_IFLNK | 0o777 if name == link_member else stat_module.S_IFREG | 0o644
                info.external_attr = mode << 16
                zf.writestr(info, (bundle / name).read_text(encoding="utf-8").encode())
        return wheel

    def test_regular_members_pass(self, verifier, tmp_path):
        verifier._check_archive(self._wheel(tmp_path))  # must not raise

    def test_a_member_flagged_as_a_symlink_is_rejected(self, verifier, tmp_path):
        # it can carry the right bytes and satisfy both the hash and uv's probe,
        # because uv materializes it as a file — another installer may not
        with pytest.raises(verifier.VerificationError, match="not a regular file"):
            verifier._check_archive(self._wheel(tmp_path, link_member="001_initial.sql"))

    def test_members_without_unix_metadata_are_accepted(self, verifier, tmp_path):
        # the common case: many writers leave external_attr at 0
        wheel = _wheel_with_real_migrations(tmp_path)

        verifier._check_archive(wheel)  # must not raise


def _load_verifier(alias):
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "scripts" / "verify_wheel.py"
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSdistMemberPathsArePortable:
    """Every member is validated as a raw archive path, on every platform.

    Checking only the SQL members with `pathlib` left two holes: `pathlib`
    normalizes `.` away, so `./src/...` looked canonical, and a non-SQL member
    named `<root>/../../escape.txt` was never inspected at all. A published
    sdist is unpacked on machines whose path rules differ from the verifier's.
    """

    @pytest.fixture
    def verifier(self):
        return _load_verifier("verify_wheel_paths")

    def test_the_canonical_sdist_passes(self, verifier, tmp_path):
        verifier._check_sdist(_sdist_with(tmp_path, _canonical_members()))  # must not raise

    def test_a_dot_root_is_rejected(self, verifier, tmp_path):
        members = [(name.replace("synapto-0.5.1/", "./"), body, kind) for name, body, kind in _canonical_members()]

        with pytest.raises(verifier.VerificationError):
            verifier._check_sdist(_sdist_with(tmp_path, members))

    def test_a_non_sql_traversal_member_is_rejected(self, verifier, tmp_path):
        # the escape hid in a text file, which the SQL-only check never saw
        members = _canonical_members() + [("synapto-0.5.1/../../escape.txt", b"x", "file")]

        with pytest.raises(verifier.VerificationError, match="component"):
            verifier._check_sdist(_sdist_with(tmp_path, members))

    def test_an_absolute_path_member_is_rejected(self, verifier, tmp_path):
        members = _canonical_members() + [("/etc/passwd", b"x", "file")]

        with pytest.raises(verifier.VerificationError, match="absolute"):
            verifier._check_sdist(_sdist_with(tmp_path, members))

    @pytest.mark.parametrize("name", ["C:/evil.txt", "\\\\server\\share\\evil.txt"])
    def test_windows_style_paths_are_rejected(self, verifier, tmp_path, name):
        # meaningless on this host, meaningful to whoever unpacks on Windows
        members = _canonical_members() + [(name, b"x", "file")]

        with pytest.raises(verifier.VerificationError):
            verifier._check_sdist(_sdist_with(tmp_path, members))

    def test_a_backslash_member_is_rejected(self, verifier, tmp_path):
        members = _canonical_members() + [("synapto-0.5.1\\evil.txt", b"x", "file")]

        with pytest.raises(verifier.VerificationError):
            verifier._check_sdist(_sdist_with(tmp_path, members))

    def test_a_duplicate_non_sql_member_is_rejected(self, verifier, tmp_path):
        members = _canonical_members() + [
            ("synapto-0.5.1/README.md", b"first", "file"),
            ("synapto-0.5.1/README.md", b"second", "file"),
        ]

        with pytest.raises(verifier.VerificationError, match="collide"):
            verifier._check_sdist(_sdist_with(tmp_path, members))

    def test_a_non_sql_symlink_is_rejected(self, verifier, tmp_path):
        members = _canonical_members() + [("synapto-0.5.1/link.txt", b"", "symlink")]

        with pytest.raises(verifier.VerificationError, match="regular file"):
            verifier._check_sdist(_sdist_with(tmp_path, members))

    def test_the_root_comes_from_the_filename_not_the_members(self, verifier, tmp_path):
        # a malicious archive controls its member names but not the artifact
        # we were asked to verify
        members = [
            (name.replace("synapto-0.5.1/", "attacker-root/"), body, kind) for name, body, kind in _canonical_members()
        ]

        with pytest.raises(verifier.VerificationError, match="outside"):
            verifier._check_sdist(_sdist_with(tmp_path, members))


class TestGzipIsConsumedToEof:
    """tarfile stops at the TAR terminator and never reads the gzip footer."""

    @pytest.fixture
    def verifier(self):
        return _load_verifier("verify_wheel_gzip")

    def _valid_sdist(self, tmp_path):
        return _sdist_with(tmp_path, _canonical_members())

    def test_a_truncated_trailer_is_rejected(self, verifier, tmp_path):
        # removing the final eight bytes leaves a structurally readable tar whose
        # CRC/ISIZE are gone; gzip -t rejects it and so must we
        sdist = self._valid_sdist(tmp_path)
        truncated = tmp_path / "truncated" / "synapto-0.5.1.tar.gz"
        truncated.parent.mkdir()
        truncated.write_bytes(sdist.read_bytes()[:-8])

        with pytest.raises(verifier.VerificationError, match="complete gzip"):
            verifier._check_sdist(truncated)

    def test_a_flipped_crc_byte_is_rejected(self, verifier, tmp_path):
        sdist = self._valid_sdist(tmp_path)
        corrupt = tmp_path / "corrupt" / "synapto-0.5.1.tar.gz"
        corrupt.parent.mkdir()
        raw = bytearray(sdist.read_bytes())
        raw[-8] ^= 0xFF
        corrupt.write_bytes(bytes(raw))

        with pytest.raises(verifier.VerificationError):
            verifier._check_sdist(corrupt)

    def test_main_returns_one_without_a_traceback(self, verifier, tmp_path, capsys):
        wheel = _wheel_with_real_migrations(tmp_path)
        sdist = self._valid_sdist(tmp_path)
        truncated = tmp_path / "broken" / "synapto-0.5.1.tar.gz"
        truncated.parent.mkdir()
        truncated.write_bytes(sdist.read_bytes()[:-8])

        assert verifier.main(["verify_wheel.py", str(wheel), str(truncated)]) == 1

        captured = capsys.readouterr()
        assert "FAILED" in captured.err
        assert "Traceback" not in captured.err

    def test_an_unrecognized_sdist_filename_is_rejected(self, verifier, tmp_path):
        odd = tmp_path / "not-an-sdist.zip"
        odd.write_bytes(b"x")

        with pytest.raises(verifier.VerificationError, match="not a recognized"):
            verifier._check_sdist(odd)


class TestCorruptCompressedWheelMember:
    @pytest.fixture
    def verifier(self):
        return _load_verifier("verify_wheel_deflate")

    def test_a_corrupted_deflate_member_is_reported(self, verifier, tmp_path, capsys):
        # zlib.error was outside the guard, so the CLI emitted a raw traceback
        from importlib import resources

        bundle = resources.files(MIGRATIONS_PACKAGE)
        wheel = tmp_path / "synapto-0.5.1-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in EXPECTED:
                zf.writestr(f"synapto/_migrations/{name}", (bundle / name).read_text(encoding="utf-8").encode())

        with zipfile.ZipFile(wheel) as zf:
            info = zf.getinfo("synapto/_migrations/001_initial.sql")
            offset = info.header_offset + 30 + len(info.filename) + len(info.extra)
        raw = bytearray(wheel.read_bytes())
        for index in range(offset + 4, offset + 12):
            raw[index] ^= 0xFF
        wheel.write_bytes(bytes(raw))

        with pytest.raises(verifier.VerificationError, match="cannot read"):
            verifier._check_archive(wheel)

        assert verifier.main(["verify_wheel.py", str(wheel)]) == 1
        assert "Traceback" not in capsys.readouterr().err


MALICIOUS_SQL = b"-- migrate:up\nDROP TABLE memories;\n-- migrate:down\nSELECT 1;\n"
CANONICAL_001 = "synapto-0.5.1/src/synapto/_migrations/001_initial.sql"


class TestExtractionAliasesAreRejected:
    """The gate must hash the same bytes the extractor will keep.

    A *regular file* named `<canonical>/` passed validation — the raw name was
    stripped before checking, so it was neither classified as SQL nor seen as a
    duplicate — and extraction dropped the trailing slash and overwrote the real
    migration. The verifier reported "checksums intact" while the extracted file
    contained DROP TABLE.
    """

    @pytest.fixture
    def verifier(self):
        return _load_verifier("verify_wheel_alias")

    def _tampered(self, tmp_path, alias_name, *, post_eof=False):
        import io
        import tarfile

        path = tmp_path / "synapto-0.5.1.tar.gz"
        path.unlink(missing_ok=True)
        with tarfile.open(path, "w:gz") as tar:
            for name, body, _kind in _canonical_members():
                info = tarfile.TarInfo(name)
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
            if post_eof:
                tar.fileobj.write(b"\0" * 1024)
            alias = tarfile.TarInfo(alias_name)
            alias.size = len(MALICIOUS_SQL)
            alias.type = tarfile.REGTYPE
            tar.addfile(alias, io.BytesIO(MALICIOUS_SQL))
        return path

    @pytest.mark.parametrize(
        "alias",
        [
            CANONICAL_001 + "/",
            CANONICAL_001 + "//",
            CANONICAL_001 + ".",
            CANONICAL_001 + " ",
            "synapto-0.5.1/src/synapto/_migrations/001_INITIAL.SQL",
            CANONICAL_001 + "/child.txt",
        ],
        ids=["slash", "double_slash", "trailing_dot", "trailing_space", "case_fold", "file_as_prefix"],
    )
    def test_an_alias_of_a_canonical_migration_is_rejected(self, verifier, tmp_path, alias):
        with pytest.raises(verifier.VerificationError):
            verifier._check_sdist(self._tampered(tmp_path, alias))

    def test_the_trailing_slash_alias_is_rejected_through_main(self, verifier, tmp_path, capsys):
        wheel = _wheel_with_real_migrations(tmp_path)
        sdist = self._tampered(tmp_path, CANONICAL_001 + "/")

        assert verifier.main(["verify_wheel.py", str(wheel), str(sdist)]) == 1
        assert "Traceback" not in capsys.readouterr().err

    def test_a_member_hidden_after_the_tar_terminator_is_seen(self, verifier, tmp_path):
        # standard extraction stops at the first TAR EOF, but tar's ignore-zero
        # behavior does not, so the hidden member must still be validated
        sdist = self._tampered(tmp_path, "synapto-0.5.1/../../SECOND_ESCAPE", post_eof=True)

        with pytest.raises(verifier.VerificationError, match="component"):
            verifier._check_sdist(sdist)


class TestWheelMemberTypeAcrossPlatforms:
    @pytest.fixture
    def verifier(self):
        return _load_verifier("verify_wheel_dos")

    def _wheel(self, tmp_path, *, create_system, external_attr):
        from importlib import resources

        bundle = resources.files(MIGRATIONS_PACKAGE)
        wheel = tmp_path / "synapto-0.5.1-py3-none-any.whl"
        wheel.unlink(missing_ok=True)
        with zipfile.ZipFile(wheel, "w") as zf:
            for name in EXPECTED:
                info = zipfile.ZipInfo(f"synapto/_migrations/{name}")
                info.create_system = create_system
                info.external_attr = external_attr
                zf.writestr(info, (bundle / name).read_text(encoding="utf-8").encode())
        return wheel

    def test_a_dos_directory_attribute_is_rejected(self, verifier, tmp_path, capsys):
        # the Unix-only check read the high bits and saw nothing, so a member
        # flagged as a DOS directory passed the gate, uv install, and the probe
        wheel = self._wheel(tmp_path, create_system=0, external_attr=0x10)

        with pytest.raises(verifier.VerificationError, match="DOS directory"):
            verifier._check_archive(wheel)

        assert verifier.main(["verify_wheel.py", str(wheel)]) == 1
        assert "Traceback" not in capsys.readouterr().err

    def test_a_dos_volume_label_is_rejected(self, verifier, tmp_path):
        wheel = self._wheel(tmp_path, create_system=0, external_attr=0x08)

        with pytest.raises(verifier.VerificationError, match="volume label"):
            verifier._check_archive(wheel)

    def test_ordinary_dos_attributes_are_accepted(self, verifier, tmp_path):
        verifier._check_archive(self._wheel(tmp_path, create_system=0, external_attr=0x20))  # archive bit

    def test_a_wheel_with_colliding_member_names_is_rejected(self, verifier, tmp_path):
        from importlib import resources

        bundle = resources.files(MIGRATIONS_PACKAGE)
        wheel = tmp_path / "synapto-0.5.1-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as zf:
            for name in EXPECTED:
                zf.writestr(f"synapto/_migrations/{name}", (bundle / name).read_text(encoding="utf-8").encode())
            zf.writestr("synapto/_migrations/001_INITIAL.SQL", MALICIOUS_SQL)

        with pytest.raises(verifier.VerificationError):
            verifier._check_archive(wheel)


class TestMalformedZipNames:
    @pytest.fixture
    def verifier(self):
        return _load_verifier("verify_wheel_unicode")

    def _wheel_with_bad_name(self, tmp_path):
        # a central-directory filename that is not valid UTF-8 while the UTF-8
        # flag is set, which makes ZipFile raise during construction
        wheel = _wheel_with_real_migrations(tmp_path)
        raw = bytearray(wheel.read_bytes())
        marker = b"synapto/_migrations/001_initial.sql"
        while (index := raw.find(marker)) != -1:
            raw[index : index + 7] = b"\xff\xfe\xfd\xfc\xfb\xfa\xf9"
        broken = tmp_path / "broken" / "synapto-0.5.1-py3-none-any.whl"
        broken.parent.mkdir(exist_ok=True)
        broken.write_bytes(bytes(raw))
        return broken

    def test_a_malformed_name_is_reported_without_a_traceback(self, verifier, tmp_path, capsys):
        broken = self._wheel_with_bad_name(tmp_path)

        assert verifier.main(["verify_wheel.py", str(broken)]) == 1

        captured = capsys.readouterr()
        assert "FAILED" in captured.err
        assert "Traceback" not in captured.err
