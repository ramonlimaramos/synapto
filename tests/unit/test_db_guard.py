"""Tests for the guards that keep destructive tests off real databases."""

from __future__ import annotations

import asyncio

import pytest

from tests.db_guard import (
    CURRENT_DATABASE_QUERY,
    PRODUCTION_DSN_ENV,
    REQUIRE_PG_ENV,
    TEST_DSN_ENV,
    GuardedPostgresClient,
    UnsafeTestDatabaseError,
    decide_test_database_action,
    is_disposable_database,
    is_pg_required,
    open_verified_client,
    resolve_test_dsn,
    verify_connection_disposable,
    verify_disposable_database,
)


class TestIsDisposableDatabase:
    @pytest.mark.parametrize("name", ["synapto_test", "anything_test", "a_test", "SYNAPTO_TEST"])
    def test_accepts_test_suffixed_names(self, name):
        assert is_disposable_database(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "synapto",  # the real database the destructive run hit
            "postgres",
            "synapto_test_backup",  # suffix must be at the end
            "test",
            "_test",  # suffix alone is not a name
            "",
            None,
            123,  # a non-string is not a name either
        ],
    )
    def test_rejects_everything_else(self, name):
        assert is_disposable_database(name) is False


class TestResolveTestDsn:
    def test_returns_configured_dsn(self):
        env = {TEST_DSN_ENV: "postgresql://localhost/synapto_test"}
        assert resolve_test_dsn(env) == "postgresql://localhost/synapto_test"

    def test_returns_none_when_unset(self):
        assert resolve_test_dsn({}) is None

    def test_returns_none_when_blank(self):
        assert resolve_test_dsn({TEST_DSN_ENV: "   "}) is None

    def test_ignores_the_production_dsn_variable(self):
        # the regression that made the destructive run possible: the suite read
        # the production variable, which every local Synapto install exports
        env = {PRODUCTION_DSN_ENV: "postgresql://localhost/synapto"}
        assert resolve_test_dsn(env) is None


class TestIsPgRequired:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " 1 "])
    def test_truthy_values_require_postgres(self, value):
        assert is_pg_required({REQUIRE_PG_ENV: value}) is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
    def test_everything_else_does_not(self, value):
        assert is_pg_required({REQUIRE_PG_ENV: value}) is False

    def test_absent_variable_does_not_require(self):
        assert is_pg_required({}) is False


class TestDecideTestDatabaseAction:
    def test_runs_when_a_test_dsn_is_configured(self):
        env = {TEST_DSN_ENV: "postgresql://localhost/synapto_test"}

        decision = decide_test_database_action(env)

        assert decision.action == "run"
        assert decision.dsn == "postgresql://localhost/synapto_test"

    def test_skips_locally_when_dsn_is_missing(self):
        # local default: pure tests still run, and no client is ever constructed
        decision = decide_test_database_action({})

        assert decision.action == "skip"
        assert decision.dsn is None
        assert TEST_DSN_ENV in decision.reason

    def test_fails_in_required_mode_when_dsn_is_missing(self):
        # CI mode: a green run with every PostgreSQL test skipped is a false pass
        decision = decide_test_database_action({REQUIRE_PG_ENV: "1"})

        assert decision.action == "fail"
        assert decision.dsn is None
        assert REQUIRE_PG_ENV in decision.reason

    def test_required_mode_still_runs_when_dsn_is_present(self):
        env = {REQUIRE_PG_ENV: "1", TEST_DSN_ENV: "postgresql://localhost/synapto_test"}

        assert decide_test_database_action(env).action == "run"

    def test_production_dsn_cannot_satisfy_required_mode(self):
        env = {REQUIRE_PG_ENV: "1", PRODUCTION_DSN_ENV: "postgresql://localhost/synapto"}

        assert decide_test_database_action(env).action == "fail"


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    async def fetchone(self):
        return self._row


class _FakeConnection:
    """Stands in for a raw psycopg connection handed to the pool's configure hook."""

    def __init__(self, row):
        self._row = row
        self.queries = []
        self.rolled_back = False

    async def execute(self, query, params=None):
        self.queries.append(query)
        return _FakeCursor(self._row)

    async def rollback(self):
        self.rolled_back = True


class _FakeClient:
    def __init__(self, row):
        self._row = row
        self.queries = []

    async def execute_one(self, query, params=None):
        self.queries.append(query)
        return self._row


class TestVerifyDisposableDatabase:
    async def test_accepts_disposable_database(self):
        assert await verify_disposable_database(_FakeClient({"name": "synapto_test"})) == "synapto_test"

    async def test_rejects_the_real_database(self):
        with pytest.raises(UnsafeTestDatabaseError, match="refusing to run destructive tests"):
            await verify_disposable_database(_FakeClient({"name": "synapto"}))

    async def test_uses_the_schema_qualified_function(self):
        # an unqualified current_database() resolves through search_path, so a
        # user-defined function in an earlier schema could spoof the guard
        client = _FakeClient({"name": "synapto_test"})

        await verify_disposable_database(client)

        assert client.queries[0] == CURRENT_DATABASE_QUERY
        assert "pg_catalog.current_database()" in client.queries[0]

    @pytest.mark.parametrize(
        "row",
        [
            None,  # no row at all
            {},  # missing key
            {"name": None},
            {"name": ""},
            {"name": 42},  # non-string
            "synapto_test",  # not a mapping
        ],
    )
    async def test_unexpected_results_are_unsafe_not_crashes(self, row):
        # fail closed: a surprise must not escape as KeyError/TypeError
        with pytest.raises(UnsafeTestDatabaseError):
            await verify_disposable_database(_FakeClient(row))


class TestVerifyConnectionDisposable:
    async def test_accepts_disposable_connection(self):
        assert await verify_connection_disposable(_FakeConnection({"name": "synapto_test"})) == "synapto_test"

    async def test_rejects_unsafe_connection(self):
        with pytest.raises(UnsafeTestDatabaseError):
            await verify_connection_disposable(_FakeConnection({"name": "synapto"}))

    async def test_uses_the_schema_qualified_function(self):
        conn = _FakeConnection({"name": "synapto_test"})

        await verify_connection_disposable(conn)

        assert conn.queries[0] == CURRENT_DATABASE_QUERY

    async def test_returns_the_connection_idle(self):
        # psycopg_pool discards any connection a configure hook leaves in
        # INTRANS; without the rollback the pool rejects every connection it
        # opens and retries until the acquire times out
        conn = _FakeConnection({"name": "synapto_test"})

        await verify_connection_disposable(conn)

        assert conn.rolled_back is True


class TestGuardedPostgresClient:
    async def test_every_new_connection_is_verified(self):
        # the pool calls configure once per physical connection, so a replacement
        # connection opened mid-suite is verified exactly like the first one
        client = GuardedPostgresClient("postgresql://localhost/synapto_test")
        conn = _FakeConnection({"name": "synapto_test"})

        await client._configure_connection(conn)

        assert conn.queries[-1] == CURRENT_DATABASE_QUERY

    async def test_unsafe_replacement_connection_cannot_run_sql(self):
        # configure raises, so the pool never hands this connection to a test:
        # no destructive statement can reach an unverified database
        client = GuardedPostgresClient("postgresql://localhost/synapto_test")
        conn = _FakeConnection({"name": "synapto"})

        with pytest.raises(UnsafeTestDatabaseError):
            await client._configure_connection(conn)

        assert conn.queries == [CURRENT_DATABASE_QUERY]


class _RecordingClient:
    """A client whose pool lifecycle we can assert on."""

    instances = []

    def __init__(self, dsn, min_size=1, max_size=2, row=None, connect_error=None):
        self.dsn = dsn
        self._row = row
        self._connect_error = connect_error
        self.connected = False
        self.closed = False
        _RecordingClient.instances.append(self)

    async def connect(self):
        if self._connect_error:
            raise self._connect_error
        self.connected = True

    async def close(self, timeout=5.0):
        self.closed = True

    async def execute_one(self, query, params=None):
        return self._row


async def _noop_precheck(dsn):
    """Stand in for the direct probe connection in unit tests."""
    return "synapto_test"


class TestOpenVerifiedClient:
    def setup_method(self):
        _RecordingClient.instances.clear()

    async def test_returns_an_open_client_for_a_disposable_database(self):
        def factory(dsn, **kwargs):
            return _RecordingClient(dsn, row={"name": "synapto_test"}, **kwargs)

        client = await open_verified_client(
            "postgresql://localhost/synapto_test", factory=factory, precheck=_noop_precheck
        )

        assert client.connected is True
        assert client.closed is False

    async def test_closes_the_pool_when_the_database_is_unsafe(self):
        def factory(dsn, **kwargs):
            return _RecordingClient(dsn, row={"name": "synapto"}, **kwargs)

        with pytest.raises(UnsafeTestDatabaseError):
            await open_verified_client("postgresql://localhost/synapto", factory=factory, precheck=_noop_precheck)

        assert _RecordingClient.instances[-1].closed is True

    async def test_closes_the_pool_when_verification_result_is_malformed(self):
        def factory(dsn, **kwargs):
            return _RecordingClient(dsn, row=None, **kwargs)

        with pytest.raises(UnsafeTestDatabaseError):
            await open_verified_client("postgresql://localhost/whatever", factory=factory, precheck=_noop_precheck)

        assert _RecordingClient.instances[-1].closed is True

    async def test_unsafe_dsn_fails_before_a_pool_is_built(self):
        # the precheck exists so a misconfigured DSN reports its reason at once,
        # instead of every pooled connection being rejected until acquire times out
        async def failing_precheck(dsn):
            raise UnsafeTestDatabaseError("refusing to run destructive tests against 'synapto'")

        def factory(dsn, **kwargs):
            return _RecordingClient(dsn, **kwargs)

        with pytest.raises(UnsafeTestDatabaseError):
            await open_verified_client(
                "postgresql://localhost/synapto", factory=factory, precheck=failing_precheck
            )

        assert _RecordingClient.instances == []

    @pytest.mark.parametrize(
        "error",
        [RuntimeError("pool refused to open"), asyncio.CancelledError()],
        ids=["runtime_error", "cancelled"],
    )
    async def test_closes_a_partially_opened_client_when_connect_fails(self, error):
        # PostgresClient assigns self._pool before awaiting pool.open(), so a
        # failure during open leaves a partial pool and its workers behind
        # unless connect() is inside the cleanup boundary
        def factory(dsn, **kwargs):
            return _RecordingClient(dsn, connect_error=error, **kwargs)

        with pytest.raises(type(error)):
            await open_verified_client(
                "postgresql://localhost/synapto_test", factory=factory, precheck=_noop_precheck
            )

        assert _RecordingClient.instances[-1].closed is True

    async def test_closes_the_pool_on_cancellation(self):
        # cancellation is not an Exception subclass; the cleanup must still run
        class _CancellingClient(_RecordingClient):
            async def execute_one(self, query, params=None):
                raise asyncio.CancelledError()

        def factory(dsn, **kwargs):
            return _CancellingClient(dsn, **kwargs)

        with pytest.raises(asyncio.CancelledError):
            await open_verified_client("postgresql://localhost/synapto_test", factory=factory, precheck=_noop_precheck)

        assert _RecordingClient.instances[-1].closed is True
