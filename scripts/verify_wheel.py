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
import lzma
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile
import zlib
from pathlib import Path

# The exact bundle this line ships, kept in step with EXPECTED in
# tests/unit/test_migration_resources.py. Adding a migration without updating
# both is what shipped a wheel nobody had verified.
EXPECTED_MIGRATIONS = {
    "001_initial.sql": "17ea18399343f8db",
    "002_add_hrr.sql": "9ed15ff823555af5",
    "003_metrics_events.sql": "ba4391fa1fd67b6e",
    "004_add_memory_subtype.sql": "9ac7f71c0e8c0300",
    "005_add_memory_domain.sql": "1ef6f84fcbc2fd65",
    "006_add_memory_scopes.sql": "b1d9bc60fed19ba0",
    "007_add_tenant_aliases.sql": "f959c16975896245",
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


_ZIP_READ_ERRORS = (
    KeyError,
    OSError,
    RuntimeError,
    NotImplementedError,
    EOFError,
    UnicodeDecodeError,
    zlib.error,
    lzma.LZMAError,
    zipfile.BadZipFile,
)


# Local file header layout (PKWARE APPNOTE 4.3.7): signature, then fixed fields,
# with the extra-field length at offset 28.
_LOCAL_HEADER_SIGNATURE = b"PK\x03\x04"
_LOCAL_HEADER_SIZE = 30
_LOCAL_EXTRA_LENGTH_OFFSET = 28


def _assert_no_extra_fields(wheel: Path, infos: list[zipfile.ZipInfo]) -> None:
    """Require every entry to carry no extra fields, central or local.

    APPNOTE 4.6.9 defines extra field 0x7075 as an *alternate* UTF-8 file name.
    A wheel can therefore present a benign name in both filename fields while a
    local-only 0x7075 remaps the entry to a canonical migration: uv reads the
    central view and keeps the real file, but bsdtar honors the local override
    and overwrites it. Neither ``ZipInfo.filename`` nor the central ``extra``
    shows that.

    A partial 0x7075 parser would be a second place to get this wrong, so this
    release gate simply refuses any extra field. The clean Hatch wheel has none,
    central or local, so nothing legitimate is lost.
    """
    for info in infos:
        if info.extra:
            raise VerificationError(f"wheel member {info.filename} carries a central extra field, which can rename it")

    try:
        with wheel.open("rb") as handle:
            for info in infos:
                handle.seek(info.header_offset)
                header = handle.read(_LOCAL_HEADER_SIZE)
                if len(header) < _LOCAL_HEADER_SIZE or not header.startswith(_LOCAL_HEADER_SIGNATURE):
                    raise VerificationError(f"wheel member {info.filename} has no valid local header")

                extra_length = int.from_bytes(
                    header[_LOCAL_EXTRA_LENGTH_OFFSET : _LOCAL_EXTRA_LENGTH_OFFSET + 2], "little"
                )
                if extra_length:
                    raise VerificationError(
                        f"wheel member {info.filename} carries a local extra field, which can rename it"
                    )
    except VerificationError:
        raise
    except OSError as exc:
        raise VerificationError(f"cannot read local headers from {wheel.name}: {exc}") from exc


def _assert_name_not_sanitized(info: zipfile.ZipInfo) -> None:
    """Reject a name Python had to repair before showing it.

    ``ZipInfo.filename`` is truncated at the first NUL while ``orig_filename``
    keeps the raw bytes, so a NUL check on ``filename`` can never fire. The
    sanitized spelling is what the gate would classify, and the raw one is what
    an extractor may act on.
    """
    if info.orig_filename != info.filename:
        raise VerificationError(
            f"wheel member {info.orig_filename!r} was normalized to {info.filename!r}; "
            "the stored name is not usable as written"
        )


def _stream_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> None:
    """Read a member to EOF so the archive has to prove it can produce it.

    Opening every member — not only the four canonical ones — is what catches a
    central-directory entry whose *local* header names something else. A wheel
    carrying a benign-looking `000_payload.bin` in the central directory and
    `001_initial.sql` in its local header passed the old gate untouched, and
    bsdtar happily extracted it over the verified migration. zipfile raises
    BadZipFile on that mismatch, but only when the member is actually opened.

    Streamed in fixed chunks so an arbitrarily large member cannot be pulled
    into memory.
    """
    try:
        with archive.open(info) as handle:
            while handle.read(1 << 16):
                pass
    except _ZIP_READ_ERRORS as exc:
        raise VerificationError(f"cannot read {info.filename} from the wheel: {exc}") from exc


def _check_archive(wheel: Path) -> None:
    """Assert the archive carries exactly the expected migration resources."""
    # opening is guarded too: a truncated or non-ZIP file raised BadZipFile
    # here, outside the read guard, and escaped main as a raw traceback
    try:
        archive = zipfile.ZipFile(wheel)
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        raise VerificationError(f"cannot open {wheel.name} as a wheel archive: {exc}") from exc

    with archive:
        try:
            infos = archive.infolist()
        except _ZIP_READ_ERRORS as exc:
            raise VerificationError(f"cannot list {wheel.name}: {exc}") from exc

        for info in infos:
            _assert_name_not_sanitized(info)
        _assert_no_extra_fields(wheel, infos)

        # the installed probe runs on one host and cannot prove how a
        # case-insensitive or Windows filesystem would resolve these names
        _reject_aliases(
            [(info.filename, _member_components(info.filename, is_dir=info.is_dir()), info.is_dir()) for info in infos]
        )

        # case-insensitive: a `.SQL` twin is still SQL to a case-insensitive
        # filesystem, and the wheel path used to compare case-sensitively
        sql_members = sorted(info.filename for info in infos if info.filename.casefold().endswith(_SQL_SUFFIXES))
        bundled = sorted(name for name in sql_members if name.startswith(RESOURCE_PREFIX))
        stray = sorted(set(sql_members) - set(bundled))
        if stray:
            raise VerificationError(f"wheel contains SQL outside {RESOURCE_PREFIX}: {stray}")

        expected = sorted(RESOURCE_PREFIX + name for name in EXPECTED_MIGRATIONS)
        if bundled != expected:
            raise VerificationError(f"wheel migration members are {bundled}, expected {expected}")

        # directory entries are streamed too: a central entry declaring a
        # directory while its local header names a canonical migration passed
        # untouched, and bsdtar then replaced that file with a directory
        for info in infos:
            if info.filename in expected:
                _assert_regular_zip_member(info)
            _stream_member(archive, info)

        # names alone would accept a wheel carrying arbitrary SQL under the right
        # filenames; the checksum is the migration's identity in the tracking
        # table, so the bytes are what must match. Hashed straight from the
        # archive: decoding and re-encoding first would verify a UTF-8 round trip
        # rather than what the wheel actually contains.
        for member in bundled:
            try:
                raw = archive.read(member)
            except _ZIP_READ_ERRORS as exc:
                raise VerificationError(f"cannot read {member} from the wheel: {exc}") from exc

            digest = hashlib.sha256(raw).hexdigest()[:16]
            name = member[len(RESOURCE_PREFIX) :]
            if digest != EXPECTED_MIGRATIONS[name]:
                raise VerificationError(f"{member} has checksum {digest}, expected {EXPECTED_MIGRATIONS[name]}")

    print(f"archive: {len(bundled)} migrations bundled under {RESOURCE_PREFIX}, every member readable, checksums match")


# Windows drive letters and UNC prefixes are meaningless on this host but
# meaningful to whoever unpacks the sdist on Windows, so they are rejected here
# rather than trusted to the extractor.
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")

# Extensions some consumer could treat as SQL. Stray detection is
# case-insensitive; the four canonical members still require exact casing.
_SQL_SUFFIXES = (".sql",)

# Characters Windows forbids outright. A name carrying one cannot be extracted
# there faithfully, so accepting it means the verified identity and the
# extracted identity may differ.
_WINDOWS_FORBIDDEN = set('<>:"|?*') | {chr(code) for code in range(0x20)}

# Reserved device names. Windows resolves them regardless of extension, so
# CON.txt is the console, not a file.
_WINDOWS_DEVICES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"}
    | {f"COM{digit}" for digit in "0123456789"}
    | {f"LPT{digit}" for digit in "0123456789"}
    # documented superscript variants of COM/LPT
    | {f"COM{sup}" for sup in "¹²³"}
    | {f"LPT{sup}" for sup in "¹²³"}
)


def _portable_key(components: list[str]) -> tuple[str, ...]:
    """Collapse a path to the identity a permissive filesystem would give it.

    Two archive members that differ only by case, or only by Windows' habit of
    discarding trailing dots and spaces, land on the same file when extracted.
    The gate hashes one of them and the extractor keeps the other, so they must
    be treated as the same name here.
    """
    return tuple(part.rstrip(". ").casefold() for part in components)


def _member_components(name: str, *, is_dir: bool) -> list[str]:
    """Split a raw archive name into components, rejecting anything unportable.

    The raw name is never normalized first. ``rstrip("/")`` used to run before
    validation, which turned a *regular file* named ``.../001_initial.sql/``
    into a look-alike of the canonical path: the gate skipped it, and extraction
    dropped the trailing slash and overwrote the real migration with its bytes.

    A non-portable spelling is rejected rather than normalized and accepted. A
    unique member named ``005_malicious.sql.`` is not a duplicate of anything,
    so a collision check cannot catch it — but Windows drops the trailing dot
    and it becomes a fifth discoverable migration.
    """
    if not name or "\x00" in name:
        raise VerificationError(f"archive member name {name!r} is empty or contains NUL")
    if name.startswith("/") or _WINDOWS_DRIVE.match(name) or name.startswith("\\\\"):
        raise VerificationError(f"archive member {name!r} is an absolute path")
    if "\\" in name:
        raise VerificationError(f"archive member {name!r} contains a backslash")

    components = name.split("/")
    # a directory entry is the only member allowed to end in a separator
    if is_dir and components and components[-1] == "":
        components = components[:-1]

    for part in components:
        if part in ("", ".", ".."):
            raise VerificationError(f"archive member {name!r} has an empty, '.' or '..' component")

        forbidden = sorted(_WINDOWS_FORBIDDEN & set(part))
        if forbidden:
            raise VerificationError(
                f"archive member {name!r} contains characters that cannot be extracted portably: {forbidden!r}"
            )

        # covers "005_malicious.sql." and components made only of dots or spaces
        if part.rstrip(" .") != part:
            raise VerificationError(
                f"archive member {name!r} has a component ending in a dot or space, which Windows silently removes"
            )

        # the Object Manager strips a leading ASCII space too, so " __init__.py"
        # aliases "__init__.py". Only U+0020, not Unicode whitespace generally.
        if part.startswith(" "):
            raise VerificationError(
                f"archive member {name!r} has a component starting with a space, which Windows silently removes"
            )

        if part.split(".", 1)[0].upper() in _WINDOWS_DEVICES:
            raise VerificationError(f"archive member {name!r} names a reserved Windows device")

    return components


def _reject_aliases(entries: list[tuple[str, list[str], bool]]) -> None:
    """Reject members that would collide once extracted.

    ``entries`` is (raw name, components, is_dir). Collisions are decided on the
    portable key, not the raw name, and a regular file that is also a prefix of
    another member's path is rejected because one of them cannot survive.
    """
    seen: dict[tuple[str, ...], str] = {}
    file_keys: set[tuple[str, ...]] = set()

    for name, components, is_dir in entries:
        key = _portable_key(components)
        if key in seen:
            raise VerificationError(f"archive members {seen[key]!r} and {name!r} collide when extracted")
        seen[key] = name
        if not is_dir:
            file_keys.add(key)

    for key, name in seen.items():
        for length in range(1, len(key)):
            if key[:length] in file_keys:
                owner = seen[key[:length]]
                raise VerificationError(f"archive member {owner!r} is a regular file and a parent of {name!r}")


def _sdist_root(sdist: Path) -> str:
    """Derive the required root from the artifact filename.

    Taken from the filename rather than inferred from the members, because a
    malicious archive controls its own member names but not what we asked to
    verify.
    """
    stem = sdist.name
    for suffix in (".tar.gz", ".tgz"):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    raise VerificationError(f"{sdist.name} is not a recognized source distribution filename")


def _assert_gzip_intact(sdist: Path) -> None:
    """Decompress the whole payload so the gzip trailer is actually checked.

    ``tarfile`` stops at the TAR terminator and never reads the gzip footer, so
    an sdist with its final eight bytes removed — which ``gzip -t`` and
    ``gzip.open().read()`` both reject — passed every check and exited 0.
    Streamed in fixed-size chunks: constant memory, one extra linear pass.
    """
    try:
        with gzip.open(sdist, "rb") as stream:
            while stream.read(1 << 16):
                pass
    except (OSError, EOFError, zlib.error, gzip.BadGzipFile) as exc:
        raise VerificationError(f"{sdist.name} is not a complete gzip archive: {exc}") from exc


# Only creator 0 is MS-DOS. APPNOTE specifies the low attribute byte for that
# system alone; creator 10 is NTFS, whose mapping is not specified there, so it
# is refused rather than guessed at for a release artifact.
_DOS_CREATOR = 0
_UNIX_CREATOR = 3

_DOS_VOLUME_LABEL = 0x08
_DOS_DIRECTORY = 0x10
_DOS_DEVICE = 0x40


def _assert_regular_zip_member(info: zipfile.ZipInfo) -> None:
    """Reject a ZIP member that is not unambiguously a regular file.

    Which half of ``external_attr`` carries meaning depends on
    ``create_system``, and reading only the Unix high bits let a member written
    with the DOS directory bit pass as a file.
    """
    dos_attributes = info.external_attr & 0xFF
    for bit, label in ((_DOS_DIRECTORY, "directory"), (_DOS_VOLUME_LABEL, "volume label"), (_DOS_DEVICE, "device")):
        if dos_attributes & bit:
            raise VerificationError(f"wheel member {info.filename} is marked as a DOS {label}")

    if info.create_system == _UNIX_CREATOR:
        file_type = (info.external_attr >> 16) & 0o170000
        if file_type and file_type != stat.S_IFREG:
            raise VerificationError(f"wheel member {info.filename} is not a regular file (type {file_type:o})")
        return

    if info.create_system == _DOS_CREATOR:
        return

    raise VerificationError(
        f"wheel member {info.filename} was created by an unsupported system "
        f"({info.create_system}); its file type cannot be verified"
    )


def _assert_tar_terminator(sdist: Path, offset: int) -> None:
    """Require two zero blocks at the TAR terminator and only padding after it.

    ``ignore_zeros`` surfaced a second *valid* TAR header, but a concatenated
    gzip member holding short opaque bytes was still skipped in silence. Reading
    the tail explicitly is the only way to say the archive ends where it claims.
    """
    try:
        with gzip.open(sdist, "rb") as stream:
            remaining = offset
            while remaining:
                chunk = stream.read(min(remaining, 1 << 16))
                if not chunk:
                    raise VerificationError(f"{sdist.name} ends before its TAR terminator")
                remaining -= len(chunk)

            terminator = stream.read(1024)
            if len(terminator) < 1024 or any(terminator):
                raise VerificationError(f"{sdist.name} lacks two complete zero blocks at the TAR terminator")

            while True:
                trailing = stream.read(1 << 16)
                if not trailing:
                    break
                if any(trailing):
                    raise VerificationError(f"{sdist.name} carries non-zero data after the TAR terminator")
    except VerificationError:
        raise
    except (OSError, EOFError, zlib.error, gzip.BadGzipFile) as exc:
        raise VerificationError(f"cannot read the tail of {sdist.name}: {exc}") from exc


def _check_sdist(sdist: Path) -> None:
    """Assert the source distribution carries exactly the four canonical migrations.

    The release builds a wheel *and* an sdist and publishes both, but only the
    wheel was gated — the sdist rode along labelled "verified". It is also what
    a downstream build compiles from, so an sdist missing or duplicating its
    migrations reproduces the original defect through another door.

    Membership is decided by exact path equality, not containment: a substring
    test accepted the four files under ``not-package/``, accepted a fifth
    duplicate beside the canonical four, and accepted ``..`` traversal.
    """
    # filename first: it is a constant-time check, and decompressing the whole
    # payload to then reject the name would be wasted work
    root = _sdist_root(sdist)
    _assert_gzip_intact(sdist)
    expected_paths = [f"{root}/src/synapto/_migrations/{name}" for name in EXPECTED_MIGRATIONS]

    try:
        with tarfile.open(sdist, "r:gz") as archive:
            infos = archive.getmembers()
            # where the parser stopped: everything from here on must be padding
            terminator_offset = archive.offset

            # every member, not just the SQL ones: a traversal escape hidden in a
            # text file is still an escape
            entries = []
            for info in infos:
                if not (info.isfile() or info.isdir()):
                    raise VerificationError(f"sdist member {info.name!r} is not a regular file or directory")
                components = _member_components(info.name, is_dir=info.isdir())
                if components[0] != root:
                    raise VerificationError(f"sdist member {info.name!r} is outside the expected root {root!r}")
                entries.append((info.name, components, info.isdir()))

            _reject_aliases(entries)

            # case-insensitive for stray detection, exact casing for the canonical
            # four: a `.SQL` twin is still SQL to a case-insensitive filesystem
            sql_members = [info for info in infos if info.name.casefold().endswith(_SQL_SUFFIXES) and info.isfile()]
            found_paths = sorted(info.name for info in sql_members)
            if found_paths != sorted(expected_paths):
                raise VerificationError(f"sdist SQL members are {found_paths}, expected {sorted(expected_paths)}")

            found = {}
            for info in sql_members:
                handle = archive.extractfile(info)
                if handle is None:
                    raise VerificationError(f"cannot read {info.name} from the sdist")
                found[info.name.rsplit("/", 1)[-1]] = hashlib.sha256(handle.read()).hexdigest()[:16]
    except VerificationError:
        raise
    except (OSError, KeyError, EOFError, zlib.error, gzip.BadGzipFile, tarfile.TarError) as exc:
        raise VerificationError(f"cannot read {sdist.name} as a source distribution: {exc}") from exc

    if found != EXPECTED_MIGRATIONS:
        raise VerificationError(f"sdist migrations are {found}, expected {EXPECTED_MIGRATIONS}")

    _assert_tar_terminator(sdist, terminator_offset)

    print(f"sdist: {len(found)} migrations bundled at {root}/src/synapto/_migrations/, gzip and terminator intact")


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
