"""Tests for memory source parsing (issue #8)."""

from __future__ import annotations

import json
from pathlib import Path

from synapto.migration.parse import parse_memory_file, parse_memory_index, parse_transcript

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# parse_memory_file
# ---------------------------------------------------------------------------


def test_parse_memory_file_extracts_frontmatter(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "feedback_style.md",
        "---\nname: code readability\ndescription: always do a readability pass\n"
        "type: feedback\n---\n\nAfter completing any implementation, review for readability.\n",
    )

    results = parse_memory_file(path)
    assert len(results) == 1
    mem = results[0]
    assert mem.memory_type == "feedback"
    assert mem.depth_layer == "core"
    assert mem.summary == "always do a readability pass"
    assert "readability" in mem.content
    assert mem.metadata["source"] == "claude-code"
    assert mem.metadata["original_name"] == "code readability"


def test_parse_memory_file_maps_types_to_depth(tmp_path: Path) -> None:
    type_depth_map = [("user", "core"), ("feedback", "core"), ("project", "working"), ("reference", "stable")]
    for mem_type, expected_depth in type_depth_map:
        path = _write(
            tmp_path / f"{mem_type}.md",
            f"---\nname: test\ntype: {mem_type}\n---\n\ncontent\n",
        )
        results = parse_memory_file(path)
        assert results[0].depth_layer == expected_depth, f"type={mem_type} should map to depth={expected_depth}"


def test_parse_memory_file_skips_empty_body(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "empty.md",
        "---\nname: empty\ntype: feedback\n---\n\n",
    )
    assert parse_memory_file(path) == []


def test_parse_memory_file_no_frontmatter(tmp_path: Path) -> None:
    path = _write(tmp_path / "plain.md", "# Just a heading\n\nSome text.")
    results = parse_memory_file(path)
    assert len(results) == 1
    assert results[0].memory_type == "general"
    assert results[0].depth_layer == "working"


def test_parse_memory_file_missing_file(tmp_path: Path) -> None:
    assert parse_memory_file(tmp_path / "nonexistent.md") == []


# ---------------------------------------------------------------------------
# parse_memory_index
# ---------------------------------------------------------------------------


def test_parse_memory_index_resolves_linked_files(tmp_path: Path) -> None:
    _write(
        tmp_path / "feedback_style.md",
        "---\nname: style\ndescription: style guide\ntype: feedback\n---\n\nUse consistent naming.\n",
    )
    _write(
        tmp_path / "ref_db.md",
        "---\nname: databases\ndescription: prod db list\ntype: reference\n---\n\nPostgres on port 5432.\n",
    )
    index = _write(
        tmp_path / "MEMORY.md",
        "- [Style Guide](feedback_style.md) — coding style\n- [Databases](ref_db.md) — production databases\n",
    )

    results = parse_memory_index(index)
    assert len(results) == 2
    types = {r.memory_type for r in results}
    assert types == {"feedback", "reference"}


def test_parse_memory_index_skips_missing_links(tmp_path: Path) -> None:
    index = _write(
        tmp_path / "MEMORY.md",
        "- [Gone](deleted.md) — this file does not exist\n",
    )
    assert parse_memory_index(index) == []


def test_parse_memory_index_fallback_for_raw_files(tmp_path: Path) -> None:
    _write(tmp_path / "notes.md", "Some raw notes without frontmatter.")
    index = _write(tmp_path / "MEMORY.md", "- [Notes](notes.md) — random notes\n")

    results = parse_memory_index(index)
    assert len(results) == 1
    assert results[0].memory_type == "general"
    assert results[0].summary == "random notes"


# ---------------------------------------------------------------------------
# parse_transcript
# ---------------------------------------------------------------------------


def _make_transcript_file(path: Path, messages: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(m) for m in messages) + "\n", encoding="utf-8")
    return path


def test_parse_transcript_extracts_user_messages(tmp_path: Path) -> None:
    msgs = [
        {"type": "user", "message": "fix the authentication bug in login.py please"},
        {"type": "assistant", "message": "I'll look at login.py now."},
        {"type": "user", "message": "also check the session timeout handling while you're at it"},
        {"type": "assistant", "message": "Found the issue in both places."},
    ]
    path = _make_transcript_file(tmp_path / "session.jsonl", msgs)

    results = parse_transcript(path)
    assert len(results) == 2
    assert all(r.memory_type == "project" for r in results)
    assert all(r.depth_layer == "working" for r in results)
    assert "authentication" in results[0].content


def test_parse_transcript_skips_short_messages(tmp_path: Path) -> None:
    msgs = [
        {"type": "user", "message": "yes"},
        {"type": "assistant", "message": "ok"},
        {"type": "user", "message": "a message that is long enough to be meaningful for import"},
    ]
    path = _make_transcript_file(tmp_path / "session.jsonl", msgs)

    results = parse_transcript(path)
    assert len(results) == 1


def test_parse_transcript_handles_nested_content(tmp_path: Path) -> None:
    msgs = [
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "text", "text": "please review the database migration script carefully"},
                ]
            },
        },
        {"type": "assistant", "message": "reviewing now"},
    ]
    path = _make_transcript_file(tmp_path / "session.jsonl", msgs)

    results = parse_transcript(path)
    assert len(results) == 1
    assert "database migration" in results[0].content


def test_parse_transcript_respects_max_messages(tmp_path: Path) -> None:
    msgs = []
    for i in range(100):
        msgs.append({"type": "user", "message": f"user message number {i} with sufficient length to pass threshold"})
        msgs.append({"type": "assistant", "message": f"reply {i}"})
    path = _make_transcript_file(tmp_path / "session.jsonl", msgs)

    results = parse_transcript(path, max_messages=10)
    assert len(results) == 10


def test_parse_transcript_missing_file(tmp_path: Path) -> None:
    assert parse_transcript(tmp_path / "nonexistent.jsonl") == []
