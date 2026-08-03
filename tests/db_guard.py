"""Guards that keep the PostgreSQL-backed test suite off real databases.

The suite runs destructive setup: the migration tests roll migration 005 down,
dropping and recreating ``memories.domain``, and the metrics backend test
truncates ``metrics_events``. On 2026-08-03 that ran against a developer's real
Synapto database — erasing live domain values and the metrics history — because
the fixture defaulted to ``postgresql://localhost/synapto`` whenever no DSN was
exported.

Two rules, both fail-closed:

1. the DSN comes only from ``SYNAPTO_TEST_PG_DSN``. The production variable
   ``SYNAPTO_PG_DSN`` is never consulted and there is no built-in default, so a
   developer with a normal Synapto setup and no test database gets a skip
   instead of a silent connection to real data.
2. the database actually connected to must be named ``*_test``, verified by
   asking the live connection rather than by parsing the DSN string — a DSN can
   omit the database name, and ``search_path``/service files can redirect it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

TEST_DSN_ENV = "SYNAPTO_TEST_PG_DSN"
PRODUCTION_DSN_ENV = "SYNAPTO_PG_DSN"
DISPOSABLE_SUFFIX = "_test"


class UnsafeTestDatabaseError(RuntimeError):
    """Raised when the connected database is not provably disposable."""


def is_disposable_database(name: str | None) -> bool:
    """Report whether ``name`` is a database the suite may destroy.

    Case-insensitive, since a quoted identifier can preserve upper case. A name
    that is only the suffix is rejected: the convention is ``<something>_test``.
    """
    if not name:
        return False
    lowered = name.lower()
    return lowered.endswith(DISPOSABLE_SUFFIX) and len(lowered) > len(DISPOSABLE_SUFFIX)


def resolve_test_dsn(env: Mapping[str, str] | None = None) -> str | None:
    """Return the configured test DSN, or ``None`` when the suite must not connect.

    Deliberately ignores :data:`PRODUCTION_DSN_ENV`. Falling back to it is what
    made the destructive run possible, and a developer running Synapto locally
    always has it set.
    """
    source = os.environ if env is None else env
    dsn = source.get(TEST_DSN_ENV, "").strip()
    return dsn or None


async def verify_disposable_database(client) -> str:
    """Assert the live connection points at a disposable database.

    Returns the database name so callers can report it.

    Raises:
        UnsafeTestDatabaseError: the connected database is not named ``*_test``.
    """
    row = await client.execute_one("SELECT current_database() AS name;")
    name = row["name"] if row else None

    if not is_disposable_database(name):
        raise UnsafeTestDatabaseError(
            f"refusing to run destructive tests against database {name!r}: "
            f"{TEST_DSN_ENV} must point at a disposable database whose name ends in "
            f"{DISPOSABLE_SUFFIX!r} (for example synapto_test). "
            "The suite drops columns and truncates tables."
        )
    return name
