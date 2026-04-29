"""Tests for synapto.telemetry.logging.configure_logging.

Validates that:
- JSON format emits parseable JSON with required fields
- Console format renders human-readable lines (not JSON)
- Log level filters lower-priority records
- contextvars bound via structlog.contextvars merge into events
- Repeated calls to configure_logging do not duplicate handlers
"""

from __future__ import annotations

import json
import logging
import sys

import pytest


@pytest.fixture(autouse=True)
def reset_logging_state():
    """Strip all handlers from root + reset structlog defaults between tests."""
    import structlog

    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level

    # Reset internal idempotency flag if module already imported
    try:
        from synapto.telemetry import logging as syn_logging

        syn_logging._configured = False
    except ImportError:
        pass

    for h in root.handlers[:]:
        root.removeHandler(h)
    structlog.reset_defaults()

    yield

    for h in root.handlers[:]:
        root.removeHandler(h)
    for h in original_handlers:
        root.addHandler(h)
    root.setLevel(original_level)
    structlog.reset_defaults()


def _read_stderr(capfd: pytest.CaptureFixture[str]) -> str:
    """Flush handlers and return captured stderr."""
    for h in logging.getLogger().handlers:
        h.flush()
    return capfd.readouterr().err


def test_json_format_emits_parseable_json(capfd: pytest.CaptureFixture[str]) -> None:
    """JSON format must produce one JSON object per log line with structured fields."""
    from synapto.telemetry.logging import configure_logging

    configure_logging(level=logging.INFO, fmt="json")
    logging.getLogger("synapto.test").info("hello world", extra={"foo": "bar"})

    err = _read_stderr(capfd)
    lines = [line for line in err.strip().split("\n") if line]
    assert lines, f"expected at least one log line, got stderr={err!r}"

    payload = json.loads(lines[-1])
    assert payload["event"] == "hello world"
    assert payload["level"] == "info"
    assert "timestamp" in payload
    # ISO 8601 UTC ends with Z or +00:00
    assert payload["timestamp"].endswith("Z") or payload["timestamp"].endswith("+00:00")


def test_console_format_human_readable(capfd: pytest.CaptureFixture[str]) -> None:
    """Console format must NOT be JSON parseable but must contain the message."""
    from synapto.telemetry.logging import configure_logging

    configure_logging(level=logging.INFO, fmt="console")
    logging.getLogger("synapto.test").info("hello console")

    err = _read_stderr(capfd)
    assert "hello console" in err
    assert "info" in err.lower()
    # Console output is not valid JSON
    with pytest.raises(json.JSONDecodeError):
        json.loads(err.strip().split("\n")[-1])


def test_log_level_filters_below_threshold(capfd: pytest.CaptureFixture[str]) -> None:
    """At WARNING level, info() must not appear; warning() must appear."""
    from synapto.telemetry.logging import configure_logging

    configure_logging(level=logging.WARNING, fmt="json")
    log = logging.getLogger("synapto.test")
    log.info("should be hidden")
    log.warning("should be visible")

    err = _read_stderr(capfd)
    assert "should be hidden" not in err
    assert "should be visible" in err


def test_contextvars_merge_into_event(capfd: pytest.CaptureFixture[str]) -> None:
    """Values bound via structlog.contextvars must appear in JSON output."""
    import structlog
    from structlog.contextvars import bind_contextvars, clear_contextvars

    from synapto.telemetry.logging import configure_logging

    configure_logging(level=logging.INFO, fmt="json")
    clear_contextvars()
    bind_contextvars(tenant="acme", request_id="req-123")
    structlog.get_logger("synapto.test").info("with context")
    clear_contextvars()

    err = _read_stderr(capfd)
    lines = [line for line in err.strip().split("\n") if line]
    payload = json.loads(lines[-1])
    assert payload["tenant"] == "acme"
    assert payload["request_id"] == "req-123"


def test_idempotent_configure_does_not_duplicate_handlers() -> None:
    """Calling configure_logging twice must leave exactly one handler on root."""
    from synapto.telemetry.logging import configure_logging

    configure_logging(level=logging.INFO, fmt="json")
    configure_logging(level=logging.INFO, fmt="json")

    root = logging.getLogger()
    handler_count = sum(1 for h in root.handlers if h.stream is sys.stderr)
    assert handler_count == 1, f"expected 1 stderr handler, got {handler_count}"
