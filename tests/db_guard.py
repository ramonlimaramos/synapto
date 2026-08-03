"""Guards that keep the PostgreSQL-backed test suite off real databases.

The suite is destructive: the migration tests roll migration 005 down, dropping
and recreating ``memories.domain``, and the metrics backend test truncates
``metrics_events``. On 2026-08-03 that ran against a developer's real Synapto
database — erasing live domain values and the metrics history — because the
fixture defaulted to ``postgresql://localhost/synapto`` whenever no DSN was
exported.

Three fail-closed rules:

1. the DSN comes only from ``SYNAPTO_TEST_PG_DSN``. The production variable
   ``SYNAPTO_PG_DSN`` is never consulted and there is no built-in default, so a
   developer with a normal Synapto setup and no test database gets a skip
   instead of a silent connection to real data.
2. **every physical connection** must prove it is attached to a database named
   ``*_test``. Verifying a single checkout before the first test proves nothing
   about the connection a later test runs on: a pool opens replacement
   connections at any time, and connection parameters can be re-resolved from
   service files or ``PGDATABASE`` in between. The check therefore lives in the
   pool's per-connection ``configure`` hook, which is the only place that sees
   all of them.
3. the name is asked of the server via ``pg_catalog.current_database()`` — schema
   qualified, because unqualified function resolution follows ``search_path`` and
   a user-defined ``current_database()`` in an earlier schema could otherwise
   spoof the guard.

``SYNAPTO_REQUIRE_TEST_PG=1`` turns a missing DSN from a skip into a failure, so
CI cannot go green with the entire PostgreSQL suite silently skipped.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

import psycopg
from psycopg.rows import dict_row

from synapto.db.postgres import PostgresClient

TEST_DSN_ENV = "SYNAPTO_TEST_PG_DSN"
PRODUCTION_DSN_ENV = "SYNAPTO_PG_DSN"
REQUIRE_PG_ENV = "SYNAPTO_REQUIRE_TEST_PG"
DISPOSABLE_SUFFIX = "_test"

# schema-qualified on purpose — see rule 3 in the module docstring
CURRENT_DATABASE_QUERY = "SELECT pg_catalog.current_database() AS name;"

_TRUTHY = {"1", "true", "yes", "on"}


class UnsafeTestDatabaseError(RuntimeError):
    """Raised when the connected database is not provably disposable."""


def is_disposable_database(name: str | None) -> bool:
    """Report whether ``name`` is a database the suite may destroy.

    Case-insensitive, since a quoted identifier can preserve upper case. A name
    that is only the suffix is rejected: the convention is ``<something>_test``.
    """
    if not name or not isinstance(name, str):
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


def is_pg_required(env: Mapping[str, str] | None = None) -> bool:
    """Report whether a missing test DSN must fail instead of skip.

    CI sets this so a misconfigured job cannot report success while every
    PostgreSQL test is skipped.
    """
    source = os.environ if env is None else env
    return source.get(REQUIRE_PG_ENV, "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class TestDatabaseDecision:
    """What the fixture should do, decided without touching the network."""

    action: Literal["run", "skip", "fail"]
    dsn: str | None = None
    reason: str | None = None


def decide_test_database_action(env: Mapping[str, str] | None = None) -> TestDatabaseDecision:
    """Decide whether to connect, skip, or fail — before constructing any client."""
    dsn = resolve_test_dsn(env)
    if dsn:
        return TestDatabaseDecision(action="run", dsn=dsn)

    if is_pg_required(env):
        return TestDatabaseDecision(
            action="fail",
            reason=(
                f"{REQUIRE_PG_ENV} is set but {TEST_DSN_ENV} is empty — the PostgreSQL "
                "tests would be skipped, which in CI means the suite reports success "
                "without exercising the database"
            ),
        )

    return TestDatabaseDecision(
        action="skip",
        reason=(
            f"{TEST_DSN_ENV} is not set — export it pointing at a disposable *_test "
            "database to run the PostgreSQL-backed tests (see 'Running the tests' in "
            "README.md)"
        ),
    )


def _database_name_from_row(row: object) -> str:
    """Extract the database name, treating any surprise as unsafe.

    A missing row, a missing key, or a non-string value must not raise
    ``KeyError``/``TypeError`` past the guard — no answer is not permission to
    proceed.
    """
    if not isinstance(row, Mapping):
        raise UnsafeTestDatabaseError(
            f"could not determine the connected database: expected a row mapping, got {type(row).__name__}"
        )

    name = row.get("name")
    if not isinstance(name, str) or not name:
        raise UnsafeTestDatabaseError(
            f"could not determine the connected database: {CURRENT_DATABASE_QUERY!r} returned {name!r}"
        )
    return name


def _assert_disposable(name: str) -> str:
    if not is_disposable_database(name):
        raise UnsafeTestDatabaseError(
            f"refusing to run destructive tests against database {name!r}: "
            f"{TEST_DSN_ENV} must point at a disposable database whose name ends in "
            f"{DISPOSABLE_SUFFIX!r} (for example synapto_test). "
            "The suite drops columns and truncates tables."
        )
    return name


async def verify_disposable_database(client) -> str:
    """Assert the database behind ``client`` is disposable. Returns its name."""
    row = await client.execute_one(CURRENT_DATABASE_QUERY)
    return _assert_disposable(_database_name_from_row(row))


async def verify_connection_disposable(conn) -> str:
    """Assert a single physical connection is attached to a disposable database.

    Runs on the raw psycopg connection, before the pool hands it to any test.
    """
    cursor = await conn.execute(CURRENT_DATABASE_QUERY)
    row = await cursor.fetchone()
    # the query opens a transaction, and psycopg_pool discards any connection a
    # configure hook leaves in INTRANS — without this the pool rejects every
    # connection it opens and retries forever
    await conn.rollback()
    return _assert_disposable(_database_name_from_row(row))


class GuardedPostgresClient(PostgresClient):
    """A :class:`PostgresClient` that verifies every physical connection.

    ``PostgresClient`` passes ``configure=self._configure_connection`` to the
    pool, and psycopg calls it once per newly opened connection. Overriding it
    turns the disposability check into an invariant of each connection rather
    than a one-time assertion about the first checkout, so a replacement
    connection cannot execute destructive SQL unverified.
    """

    async def _configure_connection(self, conn) -> None:
        await PostgresClient._configure_connection(conn)
        await verify_connection_disposable(conn)


async def verify_dsn_disposable(dsn: str) -> str:
    """Check the database on a single direct connection, before any pool exists.

    The per-connection hook alone would make a misconfigured DSN surface as a
    pool acquire timeout — every connection rejected, retried until the deadline
    — which takes ~30s per test and hides the reason. Checking first turns that
    into an immediate, named failure.
    """
    conn = await psycopg.AsyncConnection.connect(dsn, row_factory=dict_row)
    try:
        cursor = await conn.execute(CURRENT_DATABASE_QUERY)
        row = await cursor.fetchone()
    finally:
        await conn.close()
    return _assert_disposable(_database_name_from_row(row))


async def open_verified_client(dsn: str, factory=GuardedPostgresClient, precheck=verify_dsn_disposable):
    """Open a guarded client, closing the pool if anything goes wrong."""
    # fails fast and clearly, before a pool exists to time out
    await precheck(dsn)

    client = factory(dsn, min_size=1, max_size=2)
    await client.connect()
    try:
        # assert through the pooled path too, so the guarantee covers the client
        # the tests actually use, not just the probe connection
        await verify_disposable_database(client)
    except BaseException:
        # every exit path after the pool is open must close it: verification
        # errors, malformed results, timeouts, and cancellation alike
        await client.close()
        raise
    return client
