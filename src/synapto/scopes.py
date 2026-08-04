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

# ASCII lowercase, digits, and inner . _ - separators. Always applied with
# fullmatch(): with match() and a trailing "$", Python accepts a final newline,
# so "python\n" would have passed as canonical.
_CANONICAL_KEY = re.compile(r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?")

# Repository names are not plain identifiers. GitHub owners are alphanumeric
# with inner hyphens, but repository names may legitimately start with a dot —
# github/.github is a real, active repository — so the two segments need
# separate grammars. A repository segment must still contain at least one
# alphanumeric character, which also rules out "." and "..".
_REPO_OWNER = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
_REPO_NAME = re.compile(r"(?=[a-z0-9._-]*[a-z0-9])[a-z0-9._-]+")

_SCOPE_MAPPING_KEYS = frozenset({"type", "key"})

# Each grammar describes itself, so a rejection names the rule it broke rather
# than a generic one the caller may not even be subject to.
_GRAMMAR_DESCRIPTIONS = {
    _CANONICAL_KEY: ("keys must be ASCII lowercase letters, digits, and inner '.', '_' or '-'"),
    _REPO_OWNER: ("a repository owner must be ASCII lowercase letters, digits, and inner '-'"),
    _REPO_NAME: (
        "a repository name must be ASCII lowercase letters, digits, '.', '_' or '-', "
        "and contain at least one letter or digit"
    ),
}


class InvalidScopeError(ValueError):
    """Raised when a scope cannot be accepted as written."""


def _matches(pattern: re.Pattern[str], value: str) -> bool:
    """Anchored membership test — fullmatch, never match."""
    return pattern.fullmatch(value) is not None


def _canonical_suggestion(value: str, pattern: re.Pattern[str]) -> str | None:
    """Return the canonical spelling of ``value``, when trimming/lowering yields one.

    Checked against the grammar actually in force, not the generic one: an owner
    segment rejects underscores, so suggesting ``owner_name`` for ``Owner_Name``
    would send the caller to a second rejection — and ``.GitHub`` has the valid
    canonical form ``.github`` that the generic grammar would refuse to suggest.

    Used only to make rejections actionable — never applied on the caller's behalf.
    """
    candidate = value.strip().lower()
    if candidate and candidate != value and _matches(pattern, candidate):
        return candidate
    return None


def _validate_key_charset(
    scope_key: str, *, label: str = "scope_key", pattern: re.Pattern[str] = _CANONICAL_KEY
) -> None:
    if _matches(pattern, scope_key):
        return

    suggestion = _canonical_suggestion(scope_key, pattern)
    hint = f" — did you mean {suggestion!r}?" if suggestion else ""
    raise InvalidScopeError(
        f"{label} {scope_key!r} is not canonical: {_GRAMMAR_DESCRIPTIONS[pattern]}, "
        f"and must arrive already trimmed and lowercased{hint}"
    )


def reject_conflicting_scope_arguments(domain: str | None, scopes: ScopeSet | None) -> None:
    """Refuse a request that supplies both the legacy and the typed axis.

    Composing them would silently AND two different applicability models, and
    picking one would silently ignore what the caller asked for. An explicitly
    empty ``ScopeSet`` still counts as supplied — it is a deliberate assertion
    about scopes, not an absence.

    This is about *request arguments*, not storage: legacy ``domain`` data
    coexisting with scopes on a stored row stays valid until PR-4 backfills it.
    """
    if domain is not None and scopes is not None:
        raise InvalidScopeError(
            "domain and scopes cannot be combined — 'domain' is the legacy single-value axis "
            "superseded by typed scopes; pass one or the other"
        )


@dataclass(frozen=True, order=True)
class ScopeRef:
    """One validated ``(scope_type, scope_key)`` pair.

    Frozen and ordered, so a set of refs has one deterministic rendering:
    sorting is by ``(scope_type, scope_key)``, matching the field order.

    Validation runs in ``__post_init__``, so **every** construction path is
    checked — the direct constructor, :meth:`parse`, ``copy``/``replace``, and
    rows rehydrated from the database alike. An invalid ``ScopeRef`` cannot
    exist, so no consumer has to ask whether the one it holds was validated.
    """

    scope_type: str
    scope_key: str

    def __post_init__(self) -> None:
        self._validate(self.scope_type, self.scope_key)

    @classmethod
    def parse(cls, scope_type: object, scope_key: object) -> ScopeRef:
        """Build a scope reference from untrusted input.

        Equivalent to the constructor — kept as the explicit entry point for
        payloads whose types are not yet known.

        Raises:
            InvalidScopeError: the type is unknown, or the key is not canonical
                for that type.
        """
        return cls(scope_type=scope_type, scope_key=scope_key)  # type: ignore[arg-type]

    @classmethod
    def _validate(cls, scope_type: object, scope_key: object) -> None:
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
            raise InvalidScopeError(f"scope key exceeds {MAX_SCOPE_KEY_CHARS} chars (got {len(scope_key)})")

        if scope_type == GLOBAL_TYPE:
            if scope_key != GLOBAL_KEY:
                raise InvalidScopeError(
                    f"scope type {GLOBAL_TYPE!r} accepts only the key {GLOBAL_KEY!r}, got {scope_key!r}"
                )
        elif scope_type == "repo":
            cls._validate_repo_key(scope_key)
        else:
            if "/" in scope_key:
                raise InvalidScopeError(f"scope type {scope_type!r} does not accept '/' in its key: {scope_key!r}")
            _validate_key_charset(scope_key)

    @staticmethod
    def _validate_repo_key(scope_key: str) -> None:
        """Require canonical ``owner/repo`` — never a URL, SSH remote, or local path."""
        segments = scope_key.split("/")
        if len(segments) != REPO_SEGMENTS or not all(segments):
            raise InvalidScopeError(
                f"repo scope key must be canonical 'owner/repo', got {scope_key!r} "
                "(URLs, SSH remotes, and filesystem paths are not accepted)"
            )
        owner, name = segments
        _validate_key_charset(owner, label="repo owner segment", pattern=_REPO_OWNER)
        _validate_key_charset(name, label="repo name segment", pattern=_REPO_NAME)


@dataclass(frozen=True)
class ScopeSet:
    """An ordered, deduplicated, validated set of scopes for one memory.

    Empty is valid and means "unscoped" — a memory that no scope filter selects,
    not an invalid one.

    Like :class:`ScopeRef`, the aggregate invariants are enforced in
    ``__post_init__``, so the direct constructor cannot produce a set that
    :meth:`parse` would have rejected. Ordering and deduplication are applied
    there too, which is why the field is normalized rather than validated: two
    sets built from the same scopes in any order are equal.
    """

    scopes: tuple[ScopeRef, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.scopes, (str, bytes, Mapping)) or not isinstance(self.scopes, Iterable):
            raise InvalidScopeError(f"scopes must be a collection of ScopeRef, got {type(self.scopes).__name__}")

        refs = tuple(self.scopes)
        for ref in refs:
            if not isinstance(ref, ScopeRef):
                raise InvalidScopeError(f"each scope must be a ScopeRef, got {type(ref).__name__}")

        unique = set(refs)
        self._validate_aggregate(unique)
        object.__setattr__(self, "scopes", tuple(sorted(unique)))

    @staticmethod
    def _validate_aggregate(refs: set[ScopeRef]) -> None:
        if len(refs) > MAX_SCOPES:
            raise InvalidScopeError(f"at most {MAX_SCOPES} unique scopes are allowed, got {len(refs)}")

        if any(ref.scope_type == GLOBAL_TYPE for ref in refs) and len(refs) > 1:
            others = ", ".join(sorted(f"{r.scope_type}:{r.scope_key}" for r in refs if r.scope_type != GLOBAL_TYPE))
            raise InvalidScopeError(
                f"scope type {GLOBAL_TYPE!r} cannot be combined with other scopes (also given: {others})"
            )

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
            raise InvalidScopeError(f"scopes must be a list of scope objects, got {type(items).__name__}")

        return cls(scopes=tuple(cls._parse_item(item) for item in items))

    def to_payload(self) -> list[dict[str, str]]:
        """Render as JSON-safe ordered mappings, for caches and transport.

        Ordering is the value object's own, so a cached payload and a freshly
        read set serialize identically — a cache hit and a miss cannot disagree.
        """
        return [{"type": ref.scope_type, "key": ref.scope_key} for ref in self.scopes]

    @classmethod
    def from_payload(cls, value: object) -> ScopeSet:
        """Rebuild from :meth:`to_payload`, tolerating payloads written before scopes existed.

        A missing or null value is an empty set rather than an error: cache
        entries and exports predating this feature must keep deserializing.
        Anything else present is validated normally — a malformed payload is a
        bug, not a legacy artifact.
        """
        if value is None:
            return cls()
        return cls.parse(value)

    @staticmethod
    def _parse_item(item: object) -> ScopeRef:
        if isinstance(item, ScopeRef):
            return item
        if not isinstance(item, Mapping):
            raise InvalidScopeError(f"each scope must be an object with 'type' and 'key', got {type(item).__name__}")

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
