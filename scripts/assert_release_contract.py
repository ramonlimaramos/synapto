#!/usr/bin/env python3
"""Decide whether a release may proceed, before anything is built.

The release workflow publishes the version ``main`` already declares; it never
writes one. That separation is what gives this check meaning — a job that bumps
the version and then confirms the version it just wrote has verified nothing.
The version is a reviewed decision that arrives through a pull request together
with its CHANGELOG entry and a regenerated ``uv.lock``, and this script is the
gate that refuses to ship when those three disagree.

Two things are asserted, both cheap and both fatal:

*Version agreement.* ``pyproject.toml`` is the declaration of record;
``src/synapto/__init__.py`` and ``uv.lock`` must repeat it exactly. ``uv.lock``
is in the set because it is the file a bump most easily forgets: the previous
in-workflow bump edited only the first two, so every non-trivial bump produced
``X``, ``X``, ``X-1`` and died here anyway.

*Tag availability.* Without a bump step, the tag is derived from the declared
version, so dispatching twice without a version bump would build and publish
before colliding. A tag already pointing at *this* commit is a re-run and is
allowed; a tag pointing anywhere else means this version was released from
different code, which no later step can undo — a PyPI upload is immutable.

Both run before the build so the cost of a mistake is seconds rather than an
artifact that cannot be withdrawn.

Usage:
    python scripts/assert_release_contract.py [repo_root]

Writes ``version=<declared>`` to ``$GITHUB_OUTPUT`` when it passes.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path

PYPROJECT = "pyproject.toml"
INIT = "src/synapto/__init__.py"
LOCK = "uv.lock"

PACKAGE_NAME = "synapto"

_INIT_VERSION = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)

CommandRunner = Callable[[Sequence[str]], str]


class ReleaseContractError(RuntimeError):
    """The declared release state is not publishable."""


def _load_toml(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except OSError as exc:
        raise ReleaseContractError(f"cannot read {path.name}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ReleaseContractError(f"{path.name} is not valid TOML: {exc}") from exc


def read_pyproject_version(root: Path) -> str:
    """Return the declaration of record."""
    data = _load_toml(root / PYPROJECT)
    version = data.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise ReleaseContractError(f"{PYPROJECT} declares no project version")
    return version


def read_init_version(root: Path) -> str:
    """Return the version the installed package reports at runtime."""
    path = root / INIT
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseContractError(f"cannot read {INIT}: {exc}") from exc

    match = _INIT_VERSION.search(source)
    if match is None:
        raise ReleaseContractError(f"{INIT} declares no __version__")
    return match.group(1)


def read_lock_version(root: Path) -> str:
    """Return the version the resolved lockfile pins for this package.

    Parsed as TOML rather than scanned line by line: the lockfile lists every
    dependency in the same shape, so a positional read is one reordering away
    from reporting a third party's version as ours.
    """
    data = _load_toml(root / LOCK)
    for package in data.get("package", []):
        if package.get("name") == PACKAGE_NAME:
            version = package.get("version")
            if not isinstance(version, str) or not version:
                raise ReleaseContractError(f"{LOCK} pins {PACKAGE_NAME} without a version")
            return version
    raise ReleaseContractError(f"{LOCK} does not pin {PACKAGE_NAME}")


def assert_versions_agree(root: Path) -> str:
    """Return the declared version, or explain which file disagrees."""
    declared = read_pyproject_version(root)

    for name, found in ((INIT, read_init_version(root)), (LOCK, read_lock_version(root))):
        if found != declared:
            raise ReleaseContractError(f"{name} declares '{found}', but {PYPROJECT} declares '{declared}'")

    return declared


def _git_ls_remote(command: Sequence[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
    except OSError as exc:
        raise ReleaseContractError(f"cannot run {command[0]}: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        raise ReleaseContractError(f"{' '.join(command)} failed: {exc.stderr.strip() or exc}") from exc
    return result.stdout


def assert_tag_is_free(version: str, commit: str, runner: CommandRunner | None = None) -> str:
    """Return the tag this release will create, if it is safe to create it.

    The remote is queried rather than the local ref namespace: a shallow or
    stale checkout would report a tag as absent that the push then rejects,
    which is the failure this check exists to move earlier.

    ``runner`` defaults to :func:`_git_ls_remote` at call time, not at
    definition time, so a test that replaces the module attribute really does
    keep the check off the network. The earlier def-time default let the
    command-line tests pass only while no tag for the declared version existed
    on the remote — they broke the moment 0.7.0 was released.
    """
    query = runner or _git_ls_remote
    tag = f"v{version}"
    output = query(["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"])

    existing = output.split("\t")[0].strip() if output.strip() else ""
    if existing and existing != commit:
        raise ReleaseContractError(
            f"{tag} already exists and points at {existing}, not {commit}; "
            "bump the version in a pull request before releasing again"
        )
    return tag


def assert_release_contract(root: Path, commit: str, runner: CommandRunner | None = None) -> str:
    version = assert_versions_agree(root)
    tag = assert_tag_is_free(version, commit, runner)
    print(f"release contract verified: {version} may be published as {tag}")
    return version


def _emit_output(version: str) -> None:
    destination = os.environ.get("GITHUB_OUTPUT")
    if not destination:
        return
    with open(destination, "a", encoding="utf-8") as handle:
        handle.write(f"version={version}\n")


def main(argv: list[str]) -> int:
    if len(argv) > 2:
        print(f"usage: {argv[0]} [repo_root]", file=sys.stderr)
        return 2

    root = Path(argv[1]) if len(argv) == 2 else Path.cwd()
    commit = os.environ.get("GITHUB_SHA", "")

    try:
        version = assert_release_contract(root, commit)
    except ReleaseContractError as exc:
        print(f"release contract FAILED: {exc}", file=sys.stderr)
        return 1

    _emit_output(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
