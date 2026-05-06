"""Tests for the Postgres-backed metrics persistence layer.

Covers:
- ``MetricsRepository`` insert / list_by_name / purge_older_than
- ``PostgresMetricsBackend.emit`` schedules a write inside the running event loop
- JSONB tags roundtrip cleanly (empty dict, multi-key dict)
- ``list_by_name`` orders rows by ``created_at DESC``
- ``purge_older_than`` only removes rows older than the threshold
"""

from __future__ import annotations

import pytest

from synapto.db.migrations import run_migrations
from synapto.db.postgres import PostgresClient

# fixtures `pg` and `provider` come from tests/unit/conftest.py


async def _truncate_metrics(pg: PostgresClient) -> None:
    """Drop all rows so each test starts from a clean slate (table is shared across tests)."""
    await pg.execute("TRUNCATE TABLE metrics_events RESTART IDENTITY;")


@pytest.fixture
async def metrics_table(pg: PostgresClient):
    """Ensure migrations are applied and metrics_events is empty before each test."""
    await run_migrations(pg)
    await _truncate_metrics(pg)
    yield pg


class TestMetricsRepository:
    async def test_insert_persists_all_columns(self, metrics_table: PostgresClient) -> None:
        from synapto.repositories.metrics import MetricsRepository

        repo = MetricsRepository(metrics_table)
        await repo.insert(name="synapto.tool.recall.calls", metric_type="counter", value=3.0, tags={"tenant": "acme"})

        rows = await metrics_table.execute("SELECT name, type, value, tags FROM metrics_events;")
        assert len(rows) == 1
        row = rows[0]
        assert row["name"] == "synapto.tool.recall.calls"
        assert row["type"] == "counter"
        assert row["value"] == pytest.approx(3.0)
        assert row["tags"] == {"tenant": "acme"}

    async def test_empty_tags_persisted_as_empty_jsonb(self, metrics_table: PostgresClient) -> None:
        from synapto.repositories.metrics import MetricsRepository

        repo = MetricsRepository(metrics_table)
        await repo.insert(name="synapto.pool.in_use", metric_type="gauge", value=5.0, tags={})

        row = await metrics_table.execute_one("SELECT tags FROM metrics_events;")
        assert row is not None
        assert row["tags"] == {}

    async def test_multi_tag_jsonb_roundtrip(self, metrics_table: PostgresClient) -> None:
        from synapto.repositories.metrics import MetricsRepository

        repo = MetricsRepository(metrics_table)
        tags = {"tenant": "acme", "outcome": "ok", "depth": "core"}
        await repo.insert(name="synapto.recall.vector_ms", metric_type="histogram", value=23.4, tags=tags)

        row = await metrics_table.execute_one("SELECT tags FROM metrics_events;")
        assert row is not None
        assert row["tags"] == tags

    async def test_list_by_name_returns_rows_in_created_at_desc(self, metrics_table: PostgresClient) -> None:
        from synapto.repositories.metrics import MetricsRepository

        repo = MetricsRepository(metrics_table)
        for i in range(3):
            await repo.insert(name="synapto.op.total", metric_type="histogram", value=float(i), tags={})

        rows = await repo.list_by_name("synapto.op.total")
        assert len(rows) == 3
        # most recent insert (value=2) must come first
        assert [r["value"] for r in rows] == [pytest.approx(2.0), pytest.approx(1.0), pytest.approx(0.0)]

    async def test_list_by_name_tiebreaks_equal_timestamps_by_id_desc(self, metrics_table: PostgresClient) -> None:
        from synapto.repositories.metrics import MetricsRepository

        repo = MetricsRepository(metrics_table)
        for i in range(3):
            await repo.insert(name="same.timestamp", metric_type="counter", value=float(i), tags={})

        await metrics_table.execute("UPDATE metrics_events SET created_at = '2026-01-01T00:00:00Z';")

        rows = await repo.list_by_name("same.timestamp")
        assert [r["value"] for r in rows] == [pytest.approx(2.0), pytest.approx(1.0), pytest.approx(0.0)]

    async def test_purge_older_than_deletes_only_old_rows(self, metrics_table: PostgresClient) -> None:
        from synapto.repositories.metrics import MetricsRepository

        repo = MetricsRepository(metrics_table)
        await repo.insert(name="old.metric", metric_type="counter", value=1.0, tags={})
        await repo.insert(name="fresh.metric", metric_type="counter", value=1.0, tags={})

        # backdate the first row by 10 days
        await metrics_table.execute(
            "UPDATE metrics_events SET created_at = now() - interval '10 days' WHERE name = 'old.metric';"
        )

        deleted = await repo.purge_older_than(days=7)
        assert deleted == 1

        remaining = await metrics_table.execute("SELECT name FROM metrics_events;")
        assert [r["name"] for r in remaining] == ["fresh.metric"]


class TestPostgresMetricsBackend:
    async def test_emit_schedules_insert_in_running_loop(self, metrics_table: PostgresClient) -> None:
        from synapto.telemetry.backends.postgres import PostgresMetricsBackend
        from synapto.telemetry.metrics import MetricEvent

        backend = PostgresMetricsBackend(metrics_table)
        backend.emit(
            MetricEvent(
                name="synapto.tool.recall.latency",
                type="histogram",
                value=42.5,
                tags={"outcome": "ok"},
            )
        )

        # close() drains pending tasks deterministically — no polling needed.
        await backend.close()

        rows = await metrics_table.execute("SELECT name, value, tags FROM metrics_events;")
        assert len(rows) == 1
        row = rows[0]
        assert row["name"] == "synapto.tool.recall.latency"
        assert row["value"] == pytest.approx(42.5)
        assert row["tags"] == {"outcome": "ok"}

    async def test_emit_keeps_strong_reference_so_task_is_not_gc_dropped(self, metrics_table: PostgresClient) -> None:
        """Without keeping a reference, asyncio may GC the task before it runs."""
        from synapto.telemetry.backends.postgres import PostgresMetricsBackend
        from synapto.telemetry.metrics import MetricEvent

        backend = PostgresMetricsBackend(metrics_table)
        backend.emit(MetricEvent(name="ref.test", type="counter", value=1.0, tags={}))

        # The task must be tracked the moment emit() returns.
        assert len(backend._tasks) == 1

        await backend.close()
        assert len(backend._tasks) == 0

    async def test_emit_drops_under_backpressure_and_records_count(
        self, metrics_table: PostgresClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Burst above MAX_INFLIGHT_TASKS must drop excess events without crashing."""
        from synapto.telemetry.backends import postgres as backend_mod
        from synapto.telemetry.metrics import MetricEvent

        monkeypatch.setattr(backend_mod, "MAX_INFLIGHT_TASKS", 2)
        backend = backend_mod.PostgresMetricsBackend(metrics_table)

        for i in range(5):
            backend.emit(MetricEvent(name="burst", type="counter", value=float(i), tags={}))

        # 2 admitted, 3 dropped.
        assert backend._dropped_count == 3
        await backend.close()

        rows = await metrics_table.execute("SELECT count(*) AS n FROM metrics_events WHERE name = 'burst';")
        assert rows[0]["n"] == 2

    async def test_emit_after_close_drops_without_scheduling(self, metrics_table: PostgresClient) -> None:
        from synapto.telemetry.backends.postgres import PostgresMetricsBackend
        from synapto.telemetry.metrics import MetricEvent

        backend = PostgresMetricsBackend(metrics_table)
        await backend.close()
        backend.emit(MetricEvent(name="after.close", type="counter", value=1.0, tags={}))

        assert backend._dropped_count == 1
        assert len(backend._tasks) == 0

        rows = await metrics_table.execute("SELECT count(*) AS n FROM metrics_events WHERE name = 'after.close';")
        assert rows[0]["n"] == 0

    async def test_close_is_idempotent_and_safe_when_empty(self, metrics_table: PostgresClient) -> None:
        from synapto.telemetry.backends.postgres import PostgresMetricsBackend

        backend = PostgresMetricsBackend(metrics_table)
        await backend.close()  # nothing in flight, must be a no-op
        await backend.close()  # second call also safe

    async def test_close_cancels_stragglers_after_timeout(
        self, metrics_table: PostgresClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        from synapto.telemetry.backends import postgres as backend_mod
        from synapto.telemetry.metrics import MetricEvent

        async def slow_insert(*args, **kwargs) -> None:
            await asyncio.sleep(10)

        monkeypatch.setattr(backend_mod, "DRAIN_TIMEOUT_SECONDS", 0.01)
        backend = backend_mod.PostgresMetricsBackend(metrics_table)
        monkeypatch.setattr(backend._repo, "insert", slow_insert)

        backend.emit(MetricEvent(name="slow.metric", type="counter", value=1.0, tags={}))
        await backend.close()

        assert backend._closed is True
        assert len(backend._tasks) == 0
