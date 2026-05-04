"""Tests for the Postgres-backed metrics persistence layer.

Covers:
- ``MetricsRepository`` insert / list_by_name / purge_older_than
- ``PostgresMetricsBackend.emit`` schedules a write inside the running event loop
- JSONB tags roundtrip cleanly (empty dict, multi-key dict)
- ``list_by_name`` orders rows by ``created_at DESC``
- ``purge_older_than`` only removes rows older than the threshold
"""

from __future__ import annotations

import asyncio

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
        await repo.insert(name="synapto.tool.recall.calls", type="counter", value=3.0, tags={"tenant": "acme"})

        rows = await metrics_table.execute("SELECT name, type, value, tags FROM metrics_events;")
        assert len(rows) == 1
        row = rows[0]
        assert row["name"] == "synapto.tool.recall.calls"
        assert row["type"] == "counter"
        assert row["value"] == pytest.approx(3.0)
        assert row["tags"] == {"tenant": "acme"}

    async def test_empty_tags_persisted_as_empty_jsonb(
        self, metrics_table: PostgresClient
    ) -> None:
        from synapto.repositories.metrics import MetricsRepository

        repo = MetricsRepository(metrics_table)
        await repo.insert(name="synapto.pool.in_use", type="gauge", value=5.0, tags={})

        row = await metrics_table.execute_one("SELECT tags FROM metrics_events;")
        assert row is not None
        assert row["tags"] == {}

    async def test_multi_tag_jsonb_roundtrip(self, metrics_table: PostgresClient) -> None:
        from synapto.repositories.metrics import MetricsRepository

        repo = MetricsRepository(metrics_table)
        tags = {"tenant": "acme", "outcome": "ok", "depth": "core"}
        await repo.insert(name="synapto.recall.vector_ms", type="histogram", value=23.4, tags=tags)

        row = await metrics_table.execute_one("SELECT tags FROM metrics_events;")
        assert row is not None
        assert row["tags"] == tags

    async def test_list_by_name_returns_rows_in_created_at_desc(
        self, metrics_table: PostgresClient
    ) -> None:
        from synapto.repositories.metrics import MetricsRepository

        repo = MetricsRepository(metrics_table)
        for i in range(3):
            await repo.insert(name="synapto.op.total", type="histogram", value=float(i), tags={})

        rows = await repo.list_by_name("synapto.op.total")
        assert len(rows) == 3
        # most recent insert (value=2) must come first
        assert [r["value"] for r in rows] == [pytest.approx(2.0), pytest.approx(1.0), pytest.approx(0.0)]

    async def test_purge_older_than_deletes_only_old_rows(
        self, metrics_table: PostgresClient
    ) -> None:
        from synapto.repositories.metrics import MetricsRepository

        repo = MetricsRepository(metrics_table)
        await repo.insert(name="old.metric", type="counter", value=1.0, tags={})
        await repo.insert(name="fresh.metric", type="counter", value=1.0, tags={})

        # backdate the first row by 10 days
        await metrics_table.execute(
            "UPDATE metrics_events SET created_at = now() - interval '10 days' WHERE name = 'old.metric';"
        )

        deleted = await repo.purge_older_than(days=7)
        assert deleted == 1

        remaining = await metrics_table.execute("SELECT name FROM metrics_events;")
        assert [r["name"] for r in remaining] == ["fresh.metric"]


class TestPostgresMetricsBackend:
    async def test_emit_schedules_insert_in_running_loop(
        self, metrics_table: PostgresClient
    ) -> None:
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

        # emit() schedules a fire-and-forget task; let it settle.
        for _ in range(20):
            rows = await metrics_table.execute("SELECT name, value, tags FROM metrics_events;")
            if rows:
                break
            await asyncio.sleep(0.05)

        assert len(rows) == 1
        row = rows[0]
        assert row["name"] == "synapto.tool.recall.latency"
        assert row["value"] == pytest.approx(42.5)
        assert row["tags"] == {"outcome": "ok"}
