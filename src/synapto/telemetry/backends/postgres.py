"""Postgres-backed metrics backend.

Implements the ``MetricsBackend`` Strategy contract by persisting each
``MetricEvent`` to the ``metrics_events`` table via ``MetricsRepository``.

Design notes:
- ``emit()`` is sync (the contract from T2) but the underlying DB call is
  async. We bridge by scheduling the insert as a background task on the
  currently running event loop. This is fire-and-forget: callers never
  block on the metrics write.
- If the backend is invoked outside of a running event loop (e.g. from a
  pure sync utility), the event is dropped with a warning rather than
  raising — observability must not break the caller.
- Exceptions in the background task are logged and swallowed for the same
  reason: a transient DB outage should degrade telemetry, not the parent
  request.

Mounted in ``server.py:_lifespan`` after ``await pg.connect()`` so the
client is ready before any tool is called.
"""

from __future__ import annotations

import asyncio
import logging

from synapto.db.postgres import PostgresClient
from synapto.repositories.metrics import MetricsRepository
from synapto.telemetry.metrics import MetricEvent

logger = logging.getLogger("synapto.telemetry.backends.postgres")


class PostgresMetricsBackend:
    """Adapter that persists metric events to the ``metrics_events`` table."""

    def __init__(self, pg: PostgresClient) -> None:
        self._repo = MetricsRepository(pg)

    def emit(self, event: MetricEvent) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "postgres metrics backend invoked without a running loop, dropping event %s",
                event.name,
            )
            return

        loop.create_task(self._safe_insert(event))

    async def _safe_insert(self, event: MetricEvent) -> None:
        try:
            await self._repo.insert(
                name=event.name,
                type=event.type,
                value=event.value,
                tags=dict(event.tags),
            )
        except Exception:
            # Telemetry must never break the request path. Log and move on.
            logger.warning("failed to persist metric %s", event.name, exc_info=True)
