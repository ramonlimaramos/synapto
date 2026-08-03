"""Tests for the guards that keep destructive tests off real databases."""

from __future__ import annotations

import pytest

from tests.db_guard import (
    PRODUCTION_DSN_ENV,
    TEST_DSN_ENV,
    UnsafeTestDatabaseError,
    is_disposable_database,
    resolve_test_dsn,
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

    def test_production_dsn_does_not_win_over_missing_test_dsn(self):
        env = {PRODUCTION_DSN_ENV: "postgresql://localhost/synapto", TEST_DSN_ENV: ""}
        assert resolve_test_dsn(env) is None


class _FakeClient:
    def __init__(self, database_name):
        self._name = database_name
        self.queries = []

    async def execute_one(self, query, params=None):
        self.queries.append(query)
        return None if self._name is None else {"name": self._name}


class TestVerifyDisposableDatabase:
    async def test_accepts_disposable_database(self):
        client = _FakeClient("synapto_test")

        assert await verify_disposable_database(client) == "synapto_test"

    async def test_rejects_the_real_database(self):
        client = _FakeClient("synapto")

        with pytest.raises(UnsafeTestDatabaseError, match="refusing to run destructive tests"):
            await verify_disposable_database(client)

    async def test_rejects_when_database_name_is_unavailable(self):
        # fail closed: no answer is not permission to proceed
        client = _FakeClient(None)

        with pytest.raises(UnsafeTestDatabaseError):
            await verify_disposable_database(client)

    async def test_asks_the_live_connection_not_the_dsn(self):
        # a DSN can omit the database, and service files can redirect it, so the
        # check must come from the server
        client = _FakeClient("synapto_test")

        await verify_disposable_database(client)

        assert "current_database()" in client.queries[0]

    async def test_error_names_the_offending_database(self):
        client = _FakeClient("production")

        with pytest.raises(UnsafeTestDatabaseError, match="'production'"):
            await verify_disposable_database(client)
