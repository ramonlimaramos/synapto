"""Parse detected memory sources into structured records ready for import.

Each parser accepts a file path and returns a list of :class:`ParsedMemory` objects
that can be passed directly to :func:`synapto.server.remember` (or the equivalent
repository call) for storage.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("synapto.migration.parse")

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

# Mapping from Claude Code memory type to Synapto depth layer.
TYPE_TO_DEPTH: dict[str, str] = {
    "user": "core",
    "feedback": "core",
    "project": "working",
    "reference": "stable",
}

DEFAULT_DEPTH = "working"


@dataclass
class ParsedMemory:
    """A single memory record extracted from a source file."""

    content: str
    memory_type: str = "general"
    depth_layer: str = DEFAULT_DEPTH
    summary: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# YAML frontmatter helpers (minimal — avoids PyYAML dependency)
# ---------------------------------------------------------------------------

_FM_FENCE = "---"
_KV_RE = re.compile(r"^(\w[\w_-]*)\s*:\s*(.+)$")


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Extract key-value pairs from ``---`` fenced YAML frontmatter.

    Returns ``(frontmatter_dict, body)`` where *body* is everything after the
    closing fence.  If no valid frontmatter is found, returns ``({}, text)``.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != _FM_FENCE:
        return {}, text

    fm: dict[str, str] = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == _FM_FENCE:
            body = "\n".join(lines[i + 1 :]).strip()
            return fm, body
        match = _KV_RE.match(line.strip())
        if match:
            fm[match.group(1).lower()] = match.group(2).strip()

    return {}, text


# ---------------------------------------------------------------------------
# Claude Code — individual memory files
# ---------------------------------------------------------------------------


def parse_memory_file(path: Path) -> list[ParsedMemory]:
    """Parse a Claude Code memory ``.md`` file with YAML frontmatter.

    Expected format::

        ---
        name: some name
        description: one-line description
        type: user | feedback | project | reference
        ---

        Memory content here...
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("cannot read %s: %s", path, exc)
        return []

    fm, body = _parse_frontmatter(text)
    if not body.strip():
        return []

    memory_type = fm.get("type", "general")
    depth = TYPE_TO_DEPTH.get(memory_type, DEFAULT_DEPTH)
    name = fm.get("name", path.stem)
    description = fm.get("description", "")

    return [
        ParsedMemory(
            content=body,
            memory_type=memory_type,
            depth_layer=depth,
            summary=description or None,
            metadata={
                "source": "claude-code",
                "original_file": str(path),
                "original_name": name,
            },
        )
    ]


# ---------------------------------------------------------------------------
# Claude Code — MEMORY.md index
# ---------------------------------------------------------------------------

_INDEX_ENTRY_RE = re.compile(r"^-\s+\[([^\]]+)\]\(([^)]+)\)\s*(?:—\s*(.+))?$")


def parse_memory_index(path: Path) -> list[ParsedMemory]:
    """Parse a Claude Code ``MEMORY.md`` index and resolve linked files.

    Each ``- [Title](file.md) — description`` entry is resolved relative to the
    index file's directory.  If the linked file has frontmatter it is parsed via
    :func:`parse_memory_file`; otherwise the raw content is imported as-is.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("cannot read index %s: %s", path, exc)
        return []

    results: list[ParsedMemory] = []
    parent = path.parent

    for line in text.split("\n"):
        match = _INDEX_ENTRY_RE.match(line.strip())
        if not match:
            continue

        title, rel_path, description = match.group(1), match.group(2), match.group(3)
        linked = parent / rel_path
        if not linked.is_file():
            logger.debug("linked file not found: %s", linked)
            continue

        parsed = parse_memory_file(linked)
        if parsed:
            for mem in parsed:
                if mem.summary is None and description:
                    mem.summary = description.strip()
            results.extend(parsed)
        else:
            # fallback: import raw content with metadata from the index entry
            try:
                content = linked.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if content:
                results.append(
                    ParsedMemory(
                        content=content,
                        memory_type="general",
                        depth_layer=DEFAULT_DEPTH,
                        summary=description.strip() if description else None,
                        metadata={
                            "source": "claude-code",
                            "original_file": str(linked),
                            "original_name": title,
                        },
                    )
                )

    logger.info("parsed %d memories from index %s", len(results), path)
    return results


# ---------------------------------------------------------------------------
# Claude Code — session transcripts
# ---------------------------------------------------------------------------

_SUMMARY_MAX_LEN = 200


def parse_transcript(path: Path, max_messages: int = 50) -> list[ParsedMemory]:
    """Extract importable memories from a Claude Code session transcript.

    The transcript is a ``.jsonl`` file where each line is a JSON object with a
    ``type`` field (``"user"`` or ``"assistant"``).  We extract user messages as
    potential memories — the user's instructions, corrections, and context are
    the most valuable part of a session transcript.

    Only the first *max_messages* user messages are processed to keep import
    sizes reasonable.

    Each message becomes a separate ``ParsedMemory`` with ``depth_layer="working"``
    (transcripts are typically session-scoped, not permanent).
    """
    results: list[ParsedMemory] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                if obj.get("type") != "user":
                    continue

                content = _extract_message_content(obj)
                if not content or len(content) < 20:
                    continue

                summary = content[:_SUMMARY_MAX_LEN] + ("..." if len(content) > _SUMMARY_MAX_LEN else "")
                results.append(
                    ParsedMemory(
                        content=content,
                        memory_type="project",
                        depth_layer="working",
                        summary=summary,
                        metadata={
                            "source": "claude-code-transcript",
                            "original_file": str(path),
                        },
                    )
                )
                if len(results) >= max_messages:
                    break
    except OSError as exc:
        logger.warning("cannot read transcript %s: %s", path, exc)

    logger.info("parsed %d messages from transcript %s", len(results), path)
    return results


def _extract_message_content(obj: dict) -> str:
    """Pull readable text from a transcript message object.

    Handles both flat ``{"message": "..."}`` and nested ``{"message":
    {"content": [{"type": "text", "text": "..."}]}}`` shapes.
    """
    msg = obj.get("message", "")
    if isinstance(msg, str):
        return msg.strip()

    if isinstance(msg, dict):
        # {"content": "text"} or {"content": [{"type": "text", "text": "..."}]}
        content = msg.get("content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
            return "\n".join(parts).strip()

    return ""
