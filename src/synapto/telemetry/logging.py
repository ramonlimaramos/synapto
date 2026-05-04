"""Structured logging configuration via structlog.

Bridges existing ``logging.getLogger("synapto.X")`` calls to structlog's
``ProcessorFormatter`` so every log line — from stdlib loggers and from
``structlog.get_logger()`` alike — emits the same structured payload.

Output is always written to ``sys.stderr``: MCP stdio servers reserve stdout
for the protocol channel, so logs MUST NOT leak there.
"""

from __future__ import annotations

import logging
import sys
from typing import Literal

import orjson
import structlog

LogFormat = Literal["json", "console"]

_configured: bool = False


def _shared_processors() -> list:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]


def _build_renderer(fmt: LogFormat):
    if fmt == "json":
        return structlog.processors.JSONRenderer(serializer=lambda obj, **_: orjson.dumps(obj).decode())
    return structlog.dev.ConsoleRenderer(colors=True)


def configure_logging(level: int = logging.INFO, fmt: LogFormat = "json") -> None:
    """Configure structlog + stdlib logging to emit structured records to stderr.

    Idempotent: subsequent calls replace the existing handler in place rather
    than stacking duplicates. Safe to invoke from CLI entry points and tests.
    """
    global _configured

    shared = _shared_processors()
    renderer = _build_renderer(fmt)

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    _configured = True
