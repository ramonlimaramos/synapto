"""Concrete ``MetricsBackend`` implementations.

The default ``LogMetricsBackend`` lives in ``synapto.telemetry.metrics`` because
it has no extra dependencies. Backends in this subpackage have heavier
dependencies (DB clients, network exporters) and are imported on demand.
"""

from __future__ import annotations

from synapto.telemetry.backends.postgres import PostgresMetricsBackend

__all__ = ["PostgresMetricsBackend"]
