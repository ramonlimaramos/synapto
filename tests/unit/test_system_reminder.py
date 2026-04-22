"""Unit tests for system-reminder wrapping in recall output (issue #16)."""

from __future__ import annotations

from synapto.prompts import load_prompt
from synapto.server import _wrap_system_reminder


def test_wrap_system_reminder_adds_opening_and_closing_tags():
    out = _wrap_system_reminder("hello world")
    assert out.startswith("<system-reminder>\n")
    assert out.endswith("\n</system-reminder>")
    assert "hello world" in out


def test_wrap_system_reminder_strips_incoming_whitespace():
    """Avoid double-blank-line artifacts when body already ends with a newline."""
    out = _wrap_system_reminder("\n\nbody text\n\n")
    assert out == "<system-reminder>\nbody text\n</system-reminder>"


def test_recall_preamble_prompt_loads_and_is_non_empty():
    content = load_prompt("recall_preamble")
    assert content.strip(), "recall_preamble must not be empty"


def test_recall_preamble_guides_llm_behavior():
    """The preamble must teach the LLM how to treat recalled memories."""
    content = load_prompt("recall_preamble")
    # These keywords are the non-negotiable behavioral cues.
    for hint in ("context", "forget", "conflict"):
        assert hint in content.lower(), f"recall_preamble should mention {hint!r}"


def test_recall_empty_prompt_loads_and_suggests_remember():
    """The empty-state prompt must nudge the LLM to consider `remember` for new info."""
    content = load_prompt("recall_empty")
    assert content.strip(), "recall_empty must not be empty"
    assert "remember" in content.lower()
