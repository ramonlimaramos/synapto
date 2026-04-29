"""Metrics primitives — registry facade, pluggable backends, and a timing context manager.

Design patterns:
    Strategy  -- ``MetricsBackend`` is the strategy contract; backends (log, postgres,
                 OpenTelemetry) are interchangeable behind it.
    Adapter   -- Each concrete backend adapts the uniform ``MetricEvent`` interface
                 to its target sink (structlog logger today, SQL inserts later).
    Facade    -- ``MetricsRegistry`` exposes ``counter`` / ``gauge`` / ``histogram``
                 to callers so they never see the event/backend plumbing.

Process-wide singleton via ``get_registry()`` keeps call sites terse; ``set_registry()``
allows tests and config-driven swaps to replace the backend without touching call
sites. ``measure()`` is the canonical async timing context manager — it auto-tags
``outcome=ok`` on success and ``outcome=error`` on exception (and re-raises).

This module deliberately stops at primitives. Per-tool decorators, sub-stage
instrumentation, and the Postgres backend land in later T2/T3/T4/T5/T6 PRs.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import structlog

MetricType = Literal["counter", "gauge", "histogram"]


@dataclass(frozen=True)
class MetricEvent:
    """Immutable event passed from registry to backend."""

    name: str
    type: MetricType
    value: float
    tags: dict[str, Any] = field(default_factory=dict)


class MetricsBackend(Protocol):
    """Strategy contract: emit a metric event to a sink."""

    def emit(self, event: MetricEvent) -> None: ...


class LogMetricsBackend:
    """Adapter that emits each metric as a structured log line via structlog.

    Relies on ``synapto.telemetry.configure_logging`` having installed the JSON
    renderer at the root handler. Each event becomes one log line:

        {"event": "metric", "metric_name": "...", "metric_type": "...",
         "value": ..., "<tag_key>": "<tag_value>", ...}
    """

    def __init__(self) -> None:
        self._log = structlog.get_logger("synapto.metrics")

    def emit(self, event: MetricEvent) -> None:
        self._log.info(
            "metric",
            metric_name=event.name,
            metric_type=event.type,
            value=event.value,
            **event.tags,
        )


class MetricsRegistry:
    """Facade for emitting counters, gauges, and histograms through a backend."""

    def __init__(self, backend: MetricsBackend) -> None:
        self._backend = backend

    def counter(self, name: str, value: float = 1, **tags: Any) -> None:
        self._backend.emit(MetricEvent(name=name, type="counter", value=float(value), tags=dict(tags)))

    def gauge(self, name: str, value: float, **tags: Any) -> None:
        self._backend.emit(MetricEvent(name=name, type="gauge", value=float(value), tags=dict(tags)))

    def histogram(self, name: str, value: float, **tags: Any) -> None:
        self._backend.emit(MetricEvent(name=name, type="histogram", value=float(value), tags=dict(tags)))


# ---------------------------------------------------------------------------
# Singleton plumbing
# ---------------------------------------------------------------------------

_registry: MetricsRegistry | None = None


def get_registry() -> MetricsRegistry:
    """Return the process-wide registry, lazily creating a default on first use."""
    global _registry
    if _registry is None:
        _registry = MetricsRegistry(backend=LogMetricsBackend())
    return _registry


def set_registry(registry: MetricsRegistry | None) -> None:
    """Replace the process-wide registry. Pass ``None`` to reset to the default factory."""
    global _registry
    _registry = registry


# ---------------------------------------------------------------------------
# measure() — async timing context manager
# ---------------------------------------------------------------------------


@asynccontextmanager
async def measure(name: str, **tags: Any):
    """Time the wrapped block and emit a histogram with ``outcome=ok|error``.

    Usage:
        async with measure("synapto.recall.total", tenant="acme"):
            await hybrid_search(...)

    On success, emits a histogram event with ``outcome=ok`` and the elapsed
    milliseconds as the value. On exception, emits with ``outcome=error`` and
    re-raises — the caller must decide how to handle the failure; the metric
    is recorded regardless so error-rate dashboards stay accurate.
    """
    start = time.perf_counter()
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        get_registry().histogram(name, elapsed_ms, outcome=outcome, **tags)
