"""Typed memory scopes — the applicability axis for governed context.

A scope answers "when does this memory apply?", as a typed ``(scope_type,
scope_key)`` pair: ``repo:ramonlimaramos/synapto``, ``language:python``,
``skill:jerry-workday``. A memory carries a set of them, so applicability is
N:N rather than the single ``domain`` string it supersedes.

Three deliberate design choices, each with a cost worth stating:

**Keys must arrive canonical; they are rejected, never repaired.** A scope is a
storage key that reads and writes must match byte-for-byte, and silently
rewriting a caller's input hides the moment two spellings become one key.
``" Python "`` is an error, not a synonym for ``python``. The cost is friction
at the boundary, so the error names the canonical form when one exists.

**Only ASCII lowercase.** Homoglyphs are the reason: Cyrillic ``о`` renders
identically to ASCII ``o``, and a scope that merely looks right is worse than
one that fails. Control and zero-width characters are rejected on the same
grounds.

**``global`` does not combine.** A memory that applies everywhere cannot also
apply somewhere in particular — accepting both would make applicability
ambiguous rather than expressive.

Alias resolution (``py`` → ``python``) is explicitly out of scope here: it is a
lookup concern that belongs above canonicalization, not inside it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

GLOBAL_TYPE = "global"
GLOBAL_KEY = "all"

SCOPE_TYPES = frozenset({GLOBAL_TYPE, "product", "repo", "language", "skill", "workflow"})

MAX_SCOPES = 20
MAX_SCOPE_KEY_CHARS = 128
MAX_SCOPE_TYPE_CHARS = 20

REPO_SEGMENTS = 2

# ASCII lowercase, digits, and inner . _ - separators. Anchored, so anything
# outside the class — whitespace, control characters, non-ASCII, uppercase — is
# rejected rather than stripped.
_CANONICAL_KEY = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")

_SCOPE_MAPPING_KEYS = frozenset({"type", "key"})


class InvalidScopeError(ValueError):
    """Raised when a scope cannot be accepted as written."""


def _canonical_suggestion(value: str) -> str | None:
    """Return the canonical spelling of ``value``, when trimming/lowering yields one.

    Used only to make rejections actionable — it is never applied on the
    caller's behalf.
    """
    candidate = value.strip().lower()
    if candidate and candidate != value and _CANONICAL_KEY.match(candidate):
        return candidate
    return None


def _validate_key_charset(scope_key: str, *, label: str = "scope_key") -> None:
    if _CANONICAL_KEY.match(scope_key):
        return

    suggestion = _canonical_suggestion(scope_key)
    hint = f" — did you mean {suggestion!r}?" if suggestion else ""
    raise InvalidScopeError(
        f"{label} {scope_key!r} is not canonical: keys must be ASCII lowercase letters, digits, "
        f"and inner '.', '_' or '-', and must arrive already trimmed and lowercased{hint}"
    )


@dataclass(frozen=True, order=True)
class ScopeRef:
    """One validated ``(scope_type, scope_key)`` pair.

    Frozen and ordered, so a set of refs has one deterministic rendering:
    sorting is by ``(scope_type, scope_key)``, matching the field order.
    Construct through :meth:`parse` — the constructor performs no validation.
    """

    scope_type: str
    scope_key: str

    @classmethod
    def parse(cls, scope_type: object, scope_key: object) -> ScopeRef:
        """Validate and build a scope reference.

        Raises:
            InvalidScopeError: the type is unknown, or the key is not canonical
                for that type.
        """
        if not isinstance(scope_type, str) or isinstance(scope_type, bool):
            raise InvalidScopeError(f"scope type must be a string, got {type(scope_type).__name__}")
        if scope_type not in SCOPE_TYPES:
            accepted = ", ".join(sorted(SCOPE_TYPES))
            raise InvalidScopeError(f"unknown scope type {scope_type!r} — accepted types are: {accepted}")

        if not isinstance(scope_key, str) or isinstance(scope_key, bool):
            raise InvalidScopeError(f"scope key must be a string, got {type(scope_key).__name__}")
        if not scope_key:
            raise InvalidScopeError(f"scope key for type {scope_type!r} must not be empty")
        if len(scope_key) > MAX_SCOPE_KEY_CHARS:
            raise InvalidScopeError(
                f"scope key exceeds {MAX_SCOPE_KEY_CHARS} chars (got {len(scope_key)})"
            )

        if scope_type == GLOBAL_TYPE:
            if scope_key != GLOBAL_KEY:
                raise InvalidScopeError(
                    f"scope type {GLOBAL_TYPE!r} accepts only the key {GLOBAL_KEY!r}, got {scope_key!r}"
                )
        elif scope_type == "repo":
            cls._validate_repo_key(scope_key)
        else:
            if "/" in scope_key:
                raise InvalidScopeError(
                    f"scope type {scope_type!r} does not accept '/' in its key: {scope_key!r}"
                )
            _validate_key_charset(scope_key)

        return cls(scope_type=scope_type, scope_key=scope_key)

    @staticmethod
    def _validate_repo_key(scope_key: str) -> None:
        """Require canonical ``owner/repo`` — never a URL, SSH remote, or local path."""
        segments = scope_key.split("/")
        if len(segments) != REPO_SEGMENTS or not all(segments):
            raise InvalidScopeError(
                f"repo scope key must be canonical 'owner/repo', got {scope_key!r} "
                "(URLs, SSH remotes, and filesystem paths are not accepted)"
            )
        for segment in segments:
            _validate_key_charset(segment, label="repo scope segment")


@dataclass(frozen=True)
class ScopeSet:
    """An ordered, deduplicated, validated set of scopes for one memory.

    Empty is valid and means "unscoped" — a memory that no scope filter selects,
    not an invalid one.
    """

    scopes: tuple[ScopeRef, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.scopes)

    def __len__(self) -> int:
        return len(self.scopes)

    def __iter__(self):
        return iter(self.scopes)

    @classmethod
    def parse(cls, items: object) -> ScopeSet:
        """Validate a collection of scopes into a deterministic set.

        Accepts :class:`ScopeRef` instances or ``{"type": ..., "key": ...}``
        mappings. Duplicates collapse before the count is checked, so a repeated
        entry cannot push a legitimate request over :data:`MAX_SCOPES`.

        Raises:
            InvalidScopeError: the payload is malformed, a scope is invalid,
                there are more than :data:`MAX_SCOPES` unique scopes, or
                ``global`` appears alongside another scope.
        """
        # None is rejected rather than treated as empty: the mutation contract
        # distinguishes an omitted/null scopes argument (preserve what is there)
        # from an explicit [] (clear it). Collapsing them here would erase that
        # distinction before the caller can act on it.
        if isinstance(items, (str, bytes, Mapping)) or not isinstance(items, Iterable):
            raise InvalidScopeError(
                f"scopes must be a list of scope objects, got {type(items).__name__}"
            )

        refs = {cls._parse_item(item) for item in items}

        if len(refs) > MAX_SCOPES:
            raise InvalidScopeError(f"at most {MAX_SCOPES} unique scopes are allowed, got {len(refs)}")

        if any(ref.scope_type == GLOBAL_TYPE for ref in refs) and len(refs) > 1:
            others = ", ".join(sorted(f"{r.scope_type}:{r.scope_key}" for r in refs if r.scope_type != GLOBAL_TYPE))
            raise InvalidScopeError(
                f"scope type {GLOBAL_TYPE!r} cannot be combined with other scopes (also given: {others})"
            )

        return cls(scopes=tuple(sorted(refs)))

    @staticmethod
    def _parse_item(item: object) -> ScopeRef:
        if isinstance(item, ScopeRef):
            return item
        if not isinstance(item, Mapping):
            raise InvalidScopeError(
                f"each scope must be an object with 'type' and 'key', got {type(item).__name__}"
            )

        unknown = set(item) - _SCOPE_MAPPING_KEYS
        if unknown:
            accepted = ", ".join(sorted(_SCOPE_MAPPING_KEYS))
            raise InvalidScopeError(
                f"unexpected scope fields: {', '.join(sorted(map(str, unknown)))} — accepted fields are: {accepted}"
            )
        missing = _SCOPE_MAPPING_KEYS - set(item)
        if missing:
            raise InvalidScopeError(f"scope is missing required field(s): {', '.join(sorted(missing))}")

        return ScopeRef.parse(item["type"], item["key"])
