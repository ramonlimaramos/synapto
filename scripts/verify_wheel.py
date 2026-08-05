#!/usr/bin/env python3
"""Prove a built wheel can actually initialize a database.

The v0.5.0 release shipped without its SQL migrations: every published wheel
from 0.1.0 through 0.5.0 contains zero ``.sql`` files, so a clean ``pip``/``uvx``
install discovered nothing and created no schema while reporting success. The
test suite never caught it because it runs from a source checkout, where the
old ``cwd`` fallback happened to find the repository's ``migrations/``
directory.

This verifier closes that gap by testing the artifact rather than the source
tree: it installs the wheel into a throwaway environment outside the repository
and asserts that migration discovery works there. CI and the release workflow
both run it, so a wheel without migrations cannot be published again.

Usage:
    python scripts/verify_wheel.py dist/synapto-0.5.1-py3-none-any.whl [dist/synapto-0.5.1.tar.gz]
"""

from __future__ import annotations

import gzip
import hashlib
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile
from pathlib import Path

# The exact bundle v0.5.1 must ship. 005/006 belong to the unreleased v0.6 line
# and must never appear here.
EXPECTED_MIGRATIONS = {
    "001_initial.sql": "17ea18399343f8db",
    "002_add_hrr.sql": "9ed15ff823555af5",
    "003_metrics_events.sql": "ba4391fa1fd67b6e",
    "004_add_memory_subtype.sql": "9ac7f71c0e8c0300",
}

RESOURCE_PREFIX = "synapto/_migrations/"

# Runs inside the throwaway environment, with -I so the repository cannot leak
# onto sys.path and mask a missing resource.
PROBE = """
import json
import sys
import synapto
from synapto.db.migrations import discover_migrations, MigrationDiscoveryError

found = discover_migrations()
print(json.dumps({
    "python": "%d.%d" % sys.version_info[:2],
    "synapto_file": synapto.__file__,
    "migrations": [[m.filename, m.checksum] for m in found],
}))
"""


class VerificationError(RuntimeError):
    """A wheel failed one of the artifact guarantees."""


def _check_archive(wheel: Path) -> None:
    """Assert the archive carries exactly the expected migration resources."""
    # opening is guarded too: a truncated or non-ZIP file raised BadZipFile
    # here, outside the read guard, and escaped main as a raw traceback
    try:
        archive = zipfile.ZipFile(wheel)
    except (OSError, zipfile.BadZipFile) as exc:
        raise VerificationError(f"cannot open {wheel.name} as a wheel archive: {exc}") from exc

    with archive:
        names = archive.namelist()
        sql_members = sorted(n for n in names if n.endswith(".sql"))
        bundled = sorted(n for n in sql_members if n.startswith(RESOURCE_PREFIX))
        stray = sorted(set(sql_members) - set(bundled))

        if stray:
            raise VerificationError(f"wheel contains SQL outside {RESOURCE_PREFIX}: {stray}")

        expected = sorted(RESOURCE_PREFIX + name for name in EXPECTED_MIGRATIONS)
        if bundled != expected:
            raise VerificationError(f"wheel migration members are {bundled}, expected {expected}")

        # names alone would accept a wheel carrying arbitrary SQL under the right
        # filenames; the checksum is the migration's identity in the tracking
        # table, so the bytes are what must match
        # Hashed straight from the archive: decoding and re-encoding first would
        # have verified a UTF-8 round-trip rather than what the wheel actually
        # contains, and invalid bytes escaped as an uncaught UnicodeDecodeError.
        for member in bundled:
            info = archive.getinfo(member)
            # A ZIP entry can declare Unix mode bits. An entry flagged as a
            # symlink may still carry the right bytes and pass both the hash and
            # uv's probe, because uv materializes it as a regular file — another
            # installer may follow the link instead. Entries without mode
            # metadata (the normal case) are left alone.
            file_type = (info.external_attr >> 16) & 0o170000
            if file_type and file_type != stat.S_IFREG:
                raise VerificationError(f"wheel member {member} is not a regular file (type {file_type:o})")

            try:
                raw = archive.read(member)
            except (KeyError, OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile) as exc:
                raise VerificationError(f"cannot read {member} from the wheel: {exc}") from exc

            digest = hashlib.sha256(raw).hexdigest()[:16]
            name = member[len(RESOURCE_PREFIX) :]
            if digest != EXPECTED_MIGRATIONS[name]:
                raise VerificationError(f"{member} has checksum {digest}, expected {EXPECTED_MIGRATIONS[name]}")

    print(f"archive: {len(bundled)} migrations bundled under {RESOURCE_PREFIX}, checksums match")


def _sdist_root(members: list) -> str:
    """Return the single top-level directory every member must live under.

    A source distribution is one rooted tree. Accepting several roots would let
    a decoy sit beside the real package, so anything else is a rejection.
    """
    roots = {name.split("/", 1)[0] for name in members if name}
    if len(roots) != 1:
        raise VerificationError(f"sdist must have exactly one top-level directory, found {sorted(roots)}")
    return roots.pop()


def _check_sdist(sdist: Path) -> None:
    """Assert the source distribution carries exactly the four canonical migrations.

    The release builds a wheel *and* an sdist and publishes both, but only the
    wheel was gated — the sdist rode along labelled "verified". It is also what
    a downstream build compiles from, so an sdist missing or duplicating its
    migrations reproduces the original defect through another door.

    Membership is decided by exact path equality, not by containment: a
    substring test accepted the four files under ``not-package/``, accepted a
    fifth duplicate beside the canonical four, and accepted ``..`` traversal.
    """
    expected_paths = None
    try:
        with tarfile.open(sdist, "r:gz") as archive:
            infos = archive.getmembers()
            root = _sdist_root([info.name for info in infos])
            expected_paths = [f"{root}/src/synapto/_migrations/{name}" for name in EXPECTED_MIGRATIONS]

            # a list, not a set: a duplicate member must not collapse away
            sql_members = [info for info in infos if info.name.endswith(".sql")]
            found_paths = [info.name for info in sql_members]

            for name in found_paths:
                parts = Path(name).parts
                if name.startswith("/") or ".." in parts or "." in parts:
                    raise VerificationError(f"sdist member {name!r} has an unsafe path")

            if sorted(found_paths) != sorted(expected_paths) or len(found_paths) != len(expected_paths):
                raise VerificationError(
                    f"sdist SQL members are {sorted(found_paths)}, expected {sorted(expected_paths)}"
                )

            found = {}
            for info in sql_members:
                # regular files only: a symlink or hardlink could point anywhere,
                # and extractfile returns None or raises for the other types
                if not info.isfile():
                    raise VerificationError(f"sdist member {info.name!r} is not a regular file")

                handle = archive.extractfile(info)
                if handle is None:
                    raise VerificationError(f"cannot read {info.name} from the sdist")
                found[info.name.rsplit("/", 1)[-1]] = hashlib.sha256(handle.read()).hexdigest()[:16]
    except VerificationError:
        raise
    except (OSError, KeyError, EOFError, gzip.BadGzipFile, tarfile.TarError) as exc:
        raise VerificationError(f"cannot read {sdist.name} as a source distribution: {exc}") from exc

    if found != EXPECTED_MIGRATIONS:
        raise VerificationError(f"sdist migrations are {found}, expected {EXPECTED_MIGRATIONS}")

    print(f"sdist: {len(found)} migrations bundled at {root}/src/synapto/_migrations/, checksums match")


def _create_environment(env_dir: Path) -> Path:
    """Build a throwaway environment and return its interpreter.

    Prefers ``uv``: when this script itself runs under ``uv run``, the managed
    interpreter's ``ensurepip`` aborts with SIGABRT, so the stdlib path cannot
    be the only option. Falls back to ``venv`` for environments without uv.
    """
    if shutil.which("uv"):
        # --python is load-bearing: without it uv picks its own newest managed
        # interpreter, so the job labelled 3.11 was actually probing 3.13 and the
        # minimum supported version went untested
        subprocess.run(
            ["uv", "venv", "--quiet", "--python", sys.executable, str(env_dir)],
            check=True,
        )
    else:
        venv.create(env_dir, with_pip=True, clear=True)

    python = env_dir / "bin" / "python"
    if not python.exists():  # Windows layout
        python = env_dir / "Scripts" / "python.exe"
    return python


def _install_wheel(python: Path, wheel: Path) -> None:
    """Install only this wheel — no dependencies, no source checkout."""
    if shutil.which("uv"):
        subprocess.run(
            ["uv", "pip", "install", "--quiet", "--python", str(python), "--no-deps", str(wheel)],
            check=True,
        )
    else:
        subprocess.run(
            [str(python), "-m", "pip", "install", "--quiet", "--no-deps", str(wheel)],
            check=True,
        )


def _probe_installed(wheel: Path, workdir: Path) -> dict:
    """Install the wheel alone and ask the installed package what it can find."""
    python = _create_environment(workdir / "venv")
    _install_wheel(python, wheel)

    # a foreign migration in the working directory must be ignored — the old
    # implementation would have read it
    foreign = workdir / "migrations"
    foreign.mkdir(exist_ok=True)
    (foreign / "999_foreign.sql").write_text("-- migrate:up\nSELECT 1;\n-- migrate:down\n")

    result = subprocess.run(
        [str(python), "-I", "-c", PROBE],
        cwd=workdir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise VerificationError(f"installed package could not discover migrations:\n{result.stderr.strip()}")

    import json

    return json.loads(result.stdout.strip().splitlines()[-1])


def _check_probe(report: dict, workdir: Path) -> None:
    expected_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    if report.get("python") != expected_python:
        raise VerificationError(
            f"probe ran under Python {report.get('python')}, expected {expected_python} — "
            "the environment did not inherit the verifying interpreter"
        )

    installed_at = Path(report["synapto_file"]).resolve()
    if workdir.resolve() not in installed_at.parents:
        raise VerificationError(f"synapto resolved to {installed_at}, outside the temporary environment")

    discovered = {name: checksum for name, checksum in report["migrations"]}
    if discovered != EXPECTED_MIGRATIONS:
        raise VerificationError(f"installed discovery returned {discovered}, expected {EXPECTED_MIGRATIONS}")

    if any(name.startswith("999") for name in discovered):
        raise VerificationError("discovery read a migration from the working directory")

    print(f"installed: discovery returned {len(discovered)} migrations with matching checksums")
    print("installed: a foreign cwd/migrations/999_foreign.sql was correctly ignored")


def verify(wheel: Path, sdist: Path | None = None) -> None:
    if not wheel.is_file():
        raise VerificationError(f"no such wheel: {wheel}")

    print(f"verifying {wheel.name}")
    _check_archive(wheel)

    if sdist is not None:
        if not sdist.is_file():
            raise VerificationError(f"no such sdist: {sdist}")
        _check_sdist(sdist)

    with tempfile.TemporaryDirectory(prefix="synapto-wheel-") as tmp:
        workdir = Path(tmp)
        _check_probe(_probe_installed(wheel, workdir), workdir)

    print("wheel verification passed")


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(f"usage: {argv[0]} <wheel> [sdist]", file=sys.stderr)
        return 2

    try:
        verify(Path(argv[1]), Path(argv[2]) if len(argv) == 3 else None)
    except (VerificationError, subprocess.CalledProcessError) as exc:
        print(f"wheel verification FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
