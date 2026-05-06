"""Postgres-backed metrics backend.

Implements the ``MetricsBackend`` Strategy contract by persisting each
``MetricEvent`` to the ``metrics_events`` table via ``MetricsRepository``.

Design notes:
- ``emit()`` is sync (the contract from T2) but the underlying DB call is
  async. We bridge by scheduling the insert as a background task on the
  currently running event loop. This is fire-and-forget: callers never
  block on the metrics write.
- A reference to every in-flight task is kept in ``_tasks`` so the asyncio
  GC cannot reclaim the coroutine before it runs. Without this, Python
  3.10+ may silently drop scheduled tasks (and emit a "Task was destroyed
  but it is pending" warning).
- The set is bounded by ``MAX_INFLIGHT_TASKS``; once that ceiling is hit,
  new events are dropped and the count is reported via ``logger.warning``.
  This protects the connection pool from being starved by a metric burst
  (e.g. an O(n^2) tool emitting hundreds of histograms per second).
- ``close()`` drains pending tasks before the underlying pool shuts down,
  so server lifespan teardown does not orphan inserts that would then
  fail against a closed connection and spam the log.
- Failures inside ``_safe_insert`` are logged and swallowed — telemetry
  must degrade, never break the parent request.

Mounted in ``server.py:_lifespan`` after ``await pg.connect()`` so the
client is ready before any tool is called.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Final

from synapto.db.postgres import PostgresClient
from synapto.repositories.metrics import MetricsRepository
from synapto.telemetry.metrics import MetricEvent

logger = logging.getLogger("synapto.telemetry.backends.postgres")

# Cap on concurrent fire-and-forget insert tasks. Above this the emit()
# call drops the event rather than enqueueing it, preserving pool capacity
# for the actual request path. 50 is small relative to a typical max pool
# of 10 connections (each task owns at most one) yet large enough to absorb
# normal bursts without losing data.
MAX_INFLIGHT_TASKS: Final[int] = 50

# Drain budget for ``close()`` — wait at most this long for in-flight inserts
# to finish before letting the pool shut down. Keeps server shutdown crisp.
DRAIN_TIMEOUT_SECONDS: Final[float] = 2.0


class PostgresMetricsBackend:
    """Adapter that persists metric events to the ``metrics_events`` table."""

    def __init__(self, pg: PostgresClient) -> None:
        self._repo = MetricsRepository(pg)
        self._tasks: set[asyncio.Task[None]] = set()
        self._dropped_count: int = 0
        self._closed: bool = False

    def emit(self, event: MetricEvent) -> None:
        if self._closed:
            self._record_drop(
                "postgres metrics backend is closed, dropping event %s",
                event.name,
            )
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "postgres metrics backend invoked without a running loop, dropping event %s",
                event.name,
            )
            return

        # Backpressure: stop scheduling once the inflight ceiling is reached.
        # Periodic warning keeps the dropped count visible without log spam.
        if len(self._tasks) >= MAX_INFLIGHT_TASKS:
            self._record_drop(
                "postgres metrics backend at capacity (%d in-flight); dropped %d total",
                len(self._tasks),
                None,
            )
            return

        task = loop.create_task(self._safe_insert(event))
        # Strong reference until the task finishes. ``discard`` is O(1) on a set
        # and tolerates re-entry from add_done_callback.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        """Wait for in-flight inserts to drain before the underlying pool closes.

        Safe to call multiple times. Tasks that exceed the drain timeout are
        cancelled rather than awaited indefinitely so server shutdown stays
        responsive.
        """
        self._closed = True
        if not self._tasks:
            return

        pending = list(self._tasks)
        done, stragglers = await asyncio.wait(pending, timeout=DRAIN_TIMEOUT_SECONDS)
        if stragglers:
            for task in stragglers:
                task.cancel()
            await asyncio.gather(*stragglers, return_exceptions=True)
            logger.warning(
                "postgres metrics backend drained %d/%d tasks within %.1fs; cancelled the rest",
                len(done),
                len(pending),
                DRAIN_TIMEOUT_SECONDS,
            )
        self._tasks.difference_update(pending)

    async def _safe_insert(self, event: MetricEvent) -> None:
        try:
            await self._repo.insert(
                name=event.name,
                metric_type=event.type,
                value=event.value,
                tags=dict(event.tags),
            )
        except Exception:
            # Telemetry must never break the request path. Log and move on.
            logger.warning("failed to persist metric %s", event.name, exc_info=True)

    def _record_drop(self, message: str, *args: object) -> None:
        self._dropped_count += 1
        if self._dropped_count == 1 or self._dropped_count % 100 == 0:
            rendered_args = tuple(self._dropped_count if arg is None else arg for arg in args)
            logger.warning(message, *rendered_args)
