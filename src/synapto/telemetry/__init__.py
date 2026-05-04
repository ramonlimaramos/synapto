"""Synapto telemetry — structured logging and metrics primitives."""

from __future__ import annotations

from synapto.telemetry.backends import PostgresMetricsBackend
from synapto.telemetry.decorators import instrumented_tool
from synapto.telemetry.logging import configure_logging
from synapto.telemetry.metrics import (
    LogMetricsBackend,
    MetricEvent,
    MetricsBackend,
    MetricsRegistry,
    get_registry,
    measure,
    set_registry,
)

__all__ = [
    "configure_logging",
    "get_registry",
    "set_registry",
    "measure",
    "instrumented_tool",
    "MetricsRegistry",
    "MetricEvent",
    "MetricsBackend",
    "LogMetricsBackend",
    "PostgresMetricsBackend",
]
