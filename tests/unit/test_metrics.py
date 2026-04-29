"""Tests for synapto.telemetry.metrics — registry, backends, and measure() context manager.

Validates the contract that:
- counter/gauge/histogram emit MetricEvent objects to the configured backend
- measure() async ctx mgr times the block, tags outcome=ok on success and
  outcome=error on exception (and reraises), and emits a histogram event
- get_registry returns a singleton; set_registry swaps it (for tests/config)
"""

from __future__ import annotations

import pytest


class RecordingBackend:
    """Stub backend that captures emitted events for assertion."""

    def __init__(self) -> None:
        self.events: list = []

    def emit(self, event) -> None:
        self.events.append(event)


@pytest.fixture
def recorder() -> RecordingBackend:
    """Provide a fresh registry backed by a RecordingBackend per test."""
    from synapto.telemetry.metrics import MetricsRegistry, set_registry

    backend = RecordingBackend()
    set_registry(MetricsRegistry(backend=backend))
    yield backend
    set_registry(None)  # restore default singleton on teardown


def test_counter_emits_event(recorder: RecordingBackend) -> None:
    from synapto.telemetry.metrics import get_registry

    get_registry().counter("synapto.tool.recall.calls", 3, tenant="acme")

    assert len(recorder.events) == 1
    evt = recorder.events[0]
    assert evt.name == "synapto.tool.recall.calls"
    assert evt.type == "counter"
    assert evt.value == 3.0
    assert evt.tags == {"tenant": "acme"}


def test_gauge_emits_event(recorder: RecordingBackend) -> None:
    from synapto.telemetry.metrics import get_registry

    get_registry().gauge("synapto.pool.in_use", 5.0)

    assert len(recorder.events) == 1
    evt = recorder.events[0]
    assert evt.name == "synapto.pool.in_use"
    assert evt.type == "gauge"
    assert evt.value == 5.0
    assert evt.tags == {}


def test_histogram_emits_event(recorder: RecordingBackend) -> None:
    from synapto.telemetry.metrics import get_registry

    get_registry().histogram("synapto.recall.vector_ms", 23.4, tenant="acme", depth="core")

    assert len(recorder.events) == 1
    evt = recorder.events[0]
    assert evt.name == "synapto.recall.vector_ms"
    assert evt.type == "histogram"
    assert evt.value == pytest.approx(23.4)
    assert evt.tags == {"tenant": "acme", "depth": "core"}


@pytest.mark.asyncio
async def test_measure_success_emits_histogram_with_outcome_ok(
    recorder: RecordingBackend,
) -> None:
    from synapto.telemetry.metrics import measure

    async with measure("synapto.op.total", tenant="acme"):
        pass

    assert len(recorder.events) == 1
    evt = recorder.events[0]
    assert evt.name == "synapto.op.total"
    assert evt.type == "histogram"
    assert evt.value >= 0.0
    assert evt.tags == {"tenant": "acme", "outcome": "ok"}


@pytest.mark.asyncio
async def test_measure_exception_emits_outcome_error_and_reraises(
    recorder: RecordingBackend,
) -> None:
    from synapto.telemetry.metrics import measure

    with pytest.raises(ValueError, match="boom"):
        async with measure("synapto.op.total", tenant="acme"):
            raise ValueError("boom")

    assert len(recorder.events) == 1
    evt = recorder.events[0]
    assert evt.name == "synapto.op.total"
    assert evt.type == "histogram"
    assert evt.value >= 0.0
    assert evt.tags == {"tenant": "acme", "outcome": "error"}


def test_set_registry_swaps_singleton() -> None:
    """get_registry returns a singleton; set_registry replaces it; set_registry(None) resets."""
    from synapto.telemetry.metrics import (
        LogMetricsBackend,
        MetricsRegistry,
        get_registry,
        set_registry,
    )

    set_registry(None)  # ensure clean default state

    # default factory yields a stable singleton
    first = get_registry()
    second = get_registry()
    assert first is second
    assert isinstance(first._backend, LogMetricsBackend)  # type: ignore[attr-defined]

    # swap to a custom registry
    custom = MetricsRegistry(backend=RecordingBackend())
    set_registry(custom)
    assert get_registry() is custom

    # reset goes back to a fresh default (not the same instance as before)
    set_registry(None)
    third = get_registry()
    assert third is not custom
    assert isinstance(third._backend, LogMetricsBackend)  # type: ignore[attr-defined]
