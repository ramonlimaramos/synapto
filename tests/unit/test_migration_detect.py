"""Tests for memory source detection (issue #8)."""

from __future__ import annotations

import json
from pathlib import Path

from synapto.migration.detect import ScanResult, detect_all, scan_claude_code_memories, scan_claude_code_transcripts

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_memory_file(base: Path, project: str, name: str, fm_type: str = "feedback") -> Path:
    """Create a Claude Code memory .md file with frontmatter."""
    mem_dir = base / ".claude" / "projects" / project / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    path = mem_dir / name
    path.write_text(
        f"---\nname: {name.replace('.md', '')}\ndescription: test memory\ntype: {fm_type}\n---\n\nSome content here.\n",
        encoding="utf-8",
    )
    return path


def _make_memory_index(base: Path, project: str, entries: list[str]) -> Path:
    """Create a MEMORY.md index linking to the given file names."""
    mem_dir = base / ".claude" / "projects" / project / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"- [{e.replace('.md', '')}]({e}) — description for {e}" for e in entries]
    path = mem_dir / "MEMORY.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _make_transcript(base: Path, project: str, name: str, user_count: int = 5) -> Path:
    """Create a minimal Claude Code session transcript."""
    proj_dir = base / ".claude" / "projects" / project
    proj_dir.mkdir(parents=True, exist_ok=True)
    path = proj_dir / name
    lines = []
    for i in range(user_count):
        msg = f"user message number {i} with enough length to pass the threshold"
        lines.append(json.dumps({"type": "user", "message": msg}))
        lines.append(json.dumps({"type": "assistant", "message": f"assistant reply {i}"}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# scan_claude_code_memories
# ---------------------------------------------------------------------------


def test_scan_memories_finds_files_with_frontmatter(tmp_path: Path) -> None:
    _make_memory_file(tmp_path, "proj-a", "feedback_style.md", "feedback")
    _make_memory_file(tmp_path, "proj-a", "project_state.md", "project")

    sources = scan_claude_code_memories(tmp_path)
    assert len(sources) == 2
    assert all(s.source_client == "claude-code" for s in sources)
    assert all(s.format == "memory-file" for s in sources)


def test_scan_memories_skips_files_without_frontmatter(tmp_path: Path) -> None:
    mem_dir = tmp_path / ".claude" / "projects" / "proj-a" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "random.md").write_text("# Just a heading\n\nNo frontmatter here.\n")

    sources = scan_claude_code_memories(tmp_path)
    assert len(sources) == 0


def test_scan_memories_detects_index(tmp_path: Path) -> None:
    _make_memory_file(tmp_path, "proj-a", "fb.md", "feedback")
    _make_memory_index(tmp_path, "proj-a", ["fb.md", "missing.md"])

    sources = scan_claude_code_memories(tmp_path)
    index_sources = [s for s in sources if s.format == "memory-index"]
    assert len(index_sources) == 1
    assert index_sources[0].estimated_count == 2


def test_scan_memories_empty_when_no_claude_dir(tmp_path: Path) -> None:
    sources = scan_claude_code_memories(tmp_path)
    assert sources == []


def test_scan_memories_extracts_project_slug(tmp_path: Path) -> None:
    _make_memory_file(tmp_path, "-Users-ramon-myproject", "ref.md", "reference")

    sources = scan_claude_code_memories(tmp_path)
    assert len(sources) == 1
    assert sources[0].metadata["project"] == "-Users-ramon-myproject"


# ---------------------------------------------------------------------------
# scan_claude_code_transcripts
# ---------------------------------------------------------------------------


def test_scan_transcripts_finds_valid_sessions(tmp_path: Path) -> None:
    _make_transcript(tmp_path, "proj-a", "session_001.jsonl", user_count=5)

    sources = scan_claude_code_transcripts(tmp_path)
    assert len(sources) == 1
    assert sources[0].format == "transcript"
    assert sources[0].estimated_count == 5


def test_scan_transcripts_skips_short_sessions(tmp_path: Path) -> None:
    _make_transcript(tmp_path, "proj-a", "tiny.jsonl", user_count=1)

    sources = scan_claude_code_transcripts(tmp_path)
    assert len(sources) == 0


def test_scan_transcripts_skips_non_jsonl(tmp_path: Path) -> None:
    proj_dir = tmp_path / ".claude" / "projects" / "proj-a"
    proj_dir.mkdir(parents=True)
    (proj_dir / "notes.txt").write_text("not a transcript")

    sources = scan_claude_code_transcripts(tmp_path)
    assert len(sources) == 0


# ---------------------------------------------------------------------------
# detect_all
# ---------------------------------------------------------------------------


def test_detect_all_combines_sources(tmp_path: Path) -> None:
    _make_memory_file(tmp_path, "proj-a", "fb.md", "feedback")
    _make_transcript(tmp_path, "proj-a", "session.jsonl", user_count=4)

    result = detect_all(tmp_path)
    assert isinstance(result, ScanResult)
    assert len(result.sources) == 2
    by_client = result.by_client()
    assert "claude-code" in by_client


def test_detect_all_empty_home(tmp_path: Path) -> None:
    result = detect_all(tmp_path)
    assert result.sources == []
    assert result.total_estimated == 0
