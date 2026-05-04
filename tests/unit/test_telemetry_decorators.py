"""Tests for synapto.telemetry.decorators.instrumented_tool.

The decorator wraps an async MCP tool and emits two metrics per invocation:
- ``synapto.tool.<name>.latency``  (histogram, via ``measure()``, auto outcome tag)
- ``synapto.tool.<name>.calls``    (counter, value=1, manual outcome tag)

Outcome propagates consistently across both metrics:
- ``ok``        -- function returned without raising
- ``error``     -- function raised a regular ``Exception``
- ``cancelled`` -- function raised ``asyncio.CancelledError``
- (no metric) -- ``KeyboardInterrupt`` / ``SystemExit`` / ``GeneratorExit`` propagate silently
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
    from synapto.telemetry.metrics import MetricsRegistry, set_registry

    backend = RecordingBackend()
    set_registry(MetricsRegistry(backend=backend))
    yield backend
    set_registry(None)


def _by_name(events: list, name: str) -> list:
    return [e for e in events if e.name == name]


@pytest.mark.asyncio
async def test_happy_path_emits_counter_and_latency(recorder: RecordingBackend) -> None:
    from synapto.telemetry.decorators import instrumented_tool

    @instrumented_tool
    async def my_tool(value: int) -> int:
        return value * 2

    result = await my_tool(21)
    assert result == 42

    latency = _by_name(recorder.events, "synapto.tool.my_tool.latency")
    calls = _by_name(recorder.events, "synapto.tool.my_tool.calls")

    assert len(latency) == 1
    assert latency[0].type == "histogram"
    assert latency[0].value >= 0.0
    assert latency[0].tags == {"outcome": "ok"}

    assert len(calls) == 1
    assert calls[0].type == "counter"
    assert calls[0].value == 1.0
    assert calls[0].tags == {"outcome": "ok"}


@pytest.mark.asyncio
async def test_exception_path_emits_outcome_error_and_reraises(
    recorder: RecordingBackend,
) -> None:
    from synapto.telemetry.decorators import instrumented_tool

    @instrumented_tool
    async def my_tool() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await my_tool()

    latency = _by_name(recorder.events, "synapto.tool.my_tool.latency")
    calls = _by_name(recorder.events, "synapto.tool.my_tool.calls")

    assert len(latency) == 1
    assert latency[0].tags == {"outcome": "error"}
    assert len(calls) == 1
    assert calls[0].tags == {"outcome": "error"}


@pytest.mark.asyncio
async def test_cancelled_path_emits_outcome_cancelled_and_reraises(
    recorder: RecordingBackend,
) -> None:
    from synapto.telemetry.decorators import instrumented_tool

    @instrumented_tool
    async def my_tool() -> None:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await my_tool()

    latency = _by_name(recorder.events, "synapto.tool.my_tool.latency")
    calls = _by_name(recorder.events, "synapto.tool.my_tool.calls")

    assert len(latency) == 1
    assert latency[0].tags == {"outcome": "cancelled"}
    assert len(calls) == 1
    assert calls[0].tags == {"outcome": "cancelled"}


@pytest.mark.asyncio
async def test_metric_name_derived_from_func_name(recorder: RecordingBackend) -> None:
    from synapto.telemetry.decorators import instrumented_tool

    @instrumented_tool
    async def find_contradictions() -> str:
        return "ok"

    await find_contradictions()

    names = {e.name for e in recorder.events}
    assert names == {
        "synapto.tool.find_contradictions.latency",
        "synapto.tool.find_contradictions.calls",
    }


def test_decorator_preserves_function_metadata() -> None:
    """functools.wraps must keep __name__ and __doc__ so FastMCP registers correctly."""
    from synapto.telemetry.decorators import instrumented_tool

    @instrumented_tool
    async def my_tool() -> str:
        """Original docstring used by MCP description discovery."""
        return "ok"

    assert my_tool.__name__ == "my_tool"
    assert my_tool.__doc__ == "Original docstring used by MCP description discovery."
