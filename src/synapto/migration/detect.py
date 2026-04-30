"""Scan the environment for existing AI agent memory files.

Implements the detection phase of issue #8: given a home directory, locate memory
files from Claude Code (memory markdown files and session transcripts) and return
structured descriptors that the parser can consume.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from synapto.migration.parse import INDEX_ENTRY_RE

logger = logging.getLogger("synapto.migration.detect")

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class MemorySource:
    """A detected file that may contain importable memories."""

    path: Path
    source_client: str
    format: str
    estimated_count: int = 1
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class ScanResult:
    """Aggregated scan output for all detected sources."""

    sources: list[MemorySource] = field(default_factory=list)

    @property
    def total_estimated(self) -> int:
        return sum(s.estimated_count for s in self.sources)

    def by_client(self) -> dict[str, list[MemorySource]]:
        groups: dict[str, list[MemorySource]] = {}
        for s in self.sources:
            groups.setdefault(s.source_client, []).append(s)
        return groups


# ---------------------------------------------------------------------------
# Claude Code — memory files (~/.claude/projects/*/memory/*.md)
# ---------------------------------------------------------------------------

_FRONTMATTER_FENCE = "---"
_REQUIRED_FRONTMATTER_KEYS = {"name", "type"}


def _has_memory_frontmatter(path: Path) -> bool:
    """Return True if *path* starts with YAML frontmatter containing memory keys."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False

    lines = text.split("\n")
    if not lines or lines[0].strip() != _FRONTMATTER_FENCE:
        return False

    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == _FRONTMATTER_FENCE:
            header_block = "\n".join(lines[1:i])
            keys = {part.split(":")[0].strip() for part in header_block.split("\n") if ":" in part}
            return _REQUIRED_FRONTMATTER_KEYS.issubset(keys)
    return False


def _count_index_entries(index_path: Path) -> int:
    """Count linkable entries in a MEMORY.md index file.

    Uses the same regex as :data:`synapto.migration.parse.INDEX_ENTRY_RE` so the
    detection estimate matches the number the parser will actually import.
    """
    try:
        text = index_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return sum(1 for line in text.split("\n") if INDEX_ENTRY_RE.match(line.strip()))


def scan_claude_code_memories(home: Path | None = None) -> list[MemorySource]:
    """Scan ``~/.claude/projects/*/memory/`` for memory markdown files.

    Returns one :class:`MemorySource` per file.  For ``MEMORY.md`` index files the
    ``estimated_count`` reflects the number of linked sections rather than 1.
    """
    root = (home or Path.home()) / ".claude" / "projects"
    if not root.is_dir():
        logger.debug("claude code projects dir not found: %s", root)
        return []

    sources: list[MemorySource] = []
    for memory_dir in root.glob("*/memory"):
        if not memory_dir.is_dir():
            continue

        project_slug = memory_dir.parent.name

        for md in sorted(memory_dir.glob("*.md")):
            if md.name == "MEMORY.md":
                count = _count_index_entries(md)
                if count > 0:
                    sources.append(
                        MemorySource(
                            path=md,
                            source_client="claude-code",
                            format="memory-index",
                            estimated_count=count,
                            metadata={"project": project_slug},
                        )
                    )
            elif _has_memory_frontmatter(md):
                sources.append(
                    MemorySource(
                        path=md,
                        source_client="claude-code",
                        format="memory-file",
                        estimated_count=1,
                        metadata={"project": project_slug},
                    )
                )

    logger.info("claude code memories: found %d files", len(sources))
    return sources


# ---------------------------------------------------------------------------
# Claude Code — transcripts (~/.claude/projects/*/*.jsonl)
# ---------------------------------------------------------------------------

_MIN_USER_MESSAGES = 3


def _is_transcript(path: Path) -> tuple[bool, int]:
    """Check if *path* looks like a Claude Code session transcript.

    Returns ``(is_valid, user_message_count)``.  A transcript is valid when it
    contains JSON objects with ``"type"`` fields matching known message roles and
    has at least :data:`_MIN_USER_MESSAGES` user turns.
    """
    user_count = 0
    has_assistant = False
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > 500:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                msg_type = obj.get("type", "")
                if msg_type == "user":
                    user_count += 1
                elif msg_type == "assistant":
                    has_assistant = True
    except OSError:
        return False, 0

    return (user_count >= _MIN_USER_MESSAGES and has_assistant), user_count


def scan_claude_code_transcripts(home: Path | None = None) -> list[MemorySource]:
    """Scan ``~/.claude/projects/*/`` for session transcript ``.jsonl`` files.

    Only files with at least :data:`_MIN_USER_MESSAGES` user messages are included.
    """
    root = (home or Path.home()) / ".claude" / "projects"
    if not root.is_dir():
        logger.debug("claude code projects dir not found: %s", root)
        return []

    sources: list[MemorySource] = []
    for project_dir in sorted(root.iterdir()):
        if not project_dir.is_dir():
            continue
        project_slug = project_dir.name
        for jsonl in sorted(project_dir.glob("*.jsonl")):
            valid, user_count = _is_transcript(jsonl)
            if valid:
                sources.append(
                    MemorySource(
                        path=jsonl,
                        source_client="claude-code",
                        format="transcript",
                        estimated_count=user_count,
                        metadata={"project": project_slug},
                    )
                )

    logger.info("claude code transcripts: found %d files", len(sources))
    return sources


# ---------------------------------------------------------------------------
# Aggregate scanner
# ---------------------------------------------------------------------------


def detect_all(home: Path | None = None) -> ScanResult:
    """Run all available scanners and return a combined :class:`ScanResult`."""
    sources: list[MemorySource] = []
    sources.extend(scan_claude_code_memories(home))
    sources.extend(scan_claude_code_transcripts(home))
    result = ScanResult(sources=sources)
    logger.info(
        "detection complete: %d sources, ~%d estimated memories",
        len(result.sources),
        result.total_estimated,
    )
    return result
