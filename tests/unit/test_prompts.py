"""Unit tests for the prompt loader (issue #11)."""

from __future__ import annotations

import pytest

from synapto.prompts import load_prompt


def test_load_prompt_returns_non_empty_string():
    content = load_prompt("server_instructions")
    assert isinstance(content, str)
    assert content.strip(), "loaded prompt must not be empty"


def test_load_prompt_is_cached():
    """Consecutive calls must return the exact same cached string object."""
    assert load_prompt("server_instructions") is load_prompt("server_instructions")


def test_load_prompt_raises_for_missing_file():
    with pytest.raises(FileNotFoundError, match="nonexistent_prompt.md"):
        load_prompt("nonexistent_prompt")
