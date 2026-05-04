"""Tests for synapto.telemetry.metrics — registry, backends, and measure() context manager.

Validates the contract that:
- counter/gauge/histogram emit MetricEvent objects to the configured backend
- MetricEvent.tags is read-only after construction (frozen=True is not enough)
- measure() async ctx mgr times the block and tags outcome=ok | error | cancelled
- measure() rejects callers trying to override the reserved ``outcome`` tag
- KeyboardInterrupt / SystemExit propagate without emitting a metric
- get_registry returns a singleton; set_registry swaps it (for tests/config)
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest


class RecordingBackend:
    """Stub backend that captures emitted events for assertion."""

    def __init__(self) -> None:
        self.events: list = []

    def emit(self, event) -> None:
        self.events.append(event)


@pytest.fixture
def recorder() -> Iterator[RecordingBackend]:
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


@pytest.mark.asyncio
async def test_measure_cancelled_emits_outcome_cancelled_and_reraises(
    recorder: RecordingBackend,
) -> None:
    """asyncio.CancelledError is normal lifecycle in MCP/async — must NOT be tagged as error."""
    from synapto.telemetry.metrics import measure

    with pytest.raises(asyncio.CancelledError):
        async with measure("synapto.op.total", tenant="acme"):
            raise asyncio.CancelledError()

    assert len(recorder.events) == 1
    evt = recorder.events[0]
    assert evt.tags == {"tenant": "acme", "outcome": "cancelled"}


@pytest.mark.asyncio
async def test_measure_keyboard_interrupt_propagates_without_emitting(
    recorder: RecordingBackend,
) -> None:
    """Interpreter-level shutdown signals must not trigger metric emission."""
    from synapto.telemetry.metrics import measure

    with pytest.raises(KeyboardInterrupt):
        async with measure("synapto.op.total", tenant="acme"):
            raise KeyboardInterrupt()

    assert recorder.events == []


@pytest.mark.asyncio
async def test_measure_rejects_reserved_outcome_tag(recorder: RecordingBackend) -> None:
    """Caller overriding ``outcome=`` would TypeError inside finally — refuse loudly up-front."""
    from synapto.telemetry.metrics import measure

    with pytest.raises(ValueError, match="reserved tag"):
        async with measure("synapto.op.total", outcome="custom"):
            pass  # pragma: no cover — never executes

    assert recorder.events == []


def test_metric_event_tags_are_read_only_after_construction() -> None:
    """frozen=True alone doesn't protect dict tags; MappingProxyType must back them."""
    from synapto.telemetry.metrics import MetricEvent

    evt = MetricEvent(name="x", type="counter", value=1.0, tags={"tenant": "acme"})

    with pytest.raises(TypeError):
        evt.tags["tenant"] = "evil"  # type: ignore[index]

    with pytest.raises(TypeError):
        evt.tags["new"] = "key"  # type: ignore[index]


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
