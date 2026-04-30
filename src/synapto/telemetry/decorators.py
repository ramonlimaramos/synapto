"""Decorators that wire instrumentation into Synapto call sites.

Today this module exposes a single decorator, ``instrumented_tool``, designed
to wrap MCP tool entry points exposed via ``@mcp.tool``. Each wrapped call
emits two metrics through the configured ``MetricsRegistry``:

    synapto.tool.<name>.latency  (histogram, via ``measure()``, ms)
    synapto.tool.<name>.calls    (counter, value=1)

Both events carry a consistent ``outcome`` tag derived from how the wrapped
coroutine resolved:

    ok         -- returned normally
    error      -- raised a regular ``Exception``
    cancelled  -- raised ``asyncio.CancelledError`` (normal MCP lifecycle)

Interpreter-level signals (``KeyboardInterrupt``, ``SystemExit``,
``GeneratorExit``) propagate without emitting any metric, mirroring
``measure()``'s behavior — emitting during teardown risks writing to a logger
that is already being closed.

Decorator order with ``@mcp.tool`` matters: ``@mcp.tool`` MUST stay outer so
FastMCP sees the instrumented wrapper at registration time and the ``meta=``
argument continues to apply to the registered tool object:

    @mcp.tool(meta=ALWAYS_LOAD_META)   # OUTER  -- registers + sets meta
    @instrumented_tool                 # INNER  -- adds metrics
    async def remember(...): ...

``functools.wraps`` preserves ``__name__`` and ``__doc__`` so FastMCP's
function-name and docstring inference still produce the expected tool name
and description.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable
from typing import TypeVar

from synapto.telemetry.metrics import get_registry, measure

T = TypeVar("T")


def instrumented_tool(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    """Wrap an async MCP tool so each invocation emits a latency histogram and a calls counter."""
    name = func.__name__
    metric_latency = f"synapto.tool.{name}.latency"
    metric_calls = f"synapto.tool.{name}.calls"

    @functools.wraps(func)
    async def wrapper(*args, **kwargs) -> T:
        outcome: str | None = None
        try:
            async with measure(metric_latency):
                result = await func(*args, **kwargs)
            outcome = "ok"
            return result
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            # Skip the counter on KeyboardInterrupt / SystemExit / GeneratorExit
            # to match measure()'s teardown semantics.
            if outcome is not None:
                get_registry().counter(metric_calls, 1, outcome=outcome)

    return wrapper
