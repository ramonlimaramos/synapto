"""Concrete ``MetricsBackend`` implementations beyond the default log backend.

The default ``LogMetricsBackend`` lives in ``synapto.telemetry.metrics`` so it
can ride along with the primitives that depend on nothing but structlog.
This subpackage groups backends with heavier dependencies (DB clients,
network exporters) so the call sites that only need the registry don't have
to know about them.

Note: the postgres backend is re-exported here for convenience and is loaded
eagerly when this package is imported. If a future backend needs lazy loading
to avoid pulling a heavy dep at import time, defer that backend's import to
its own module rather than this ``__init__``.
"""

from __future__ import annotations

from synapto.telemetry.backends.postgres import PostgresMetricsBackend

__all__ = ["PostgresMetricsBackend"]
