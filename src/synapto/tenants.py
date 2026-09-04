"""Tenant resolution — the tenant is a resolved location, not a caller's string.

The tenant is a hard partition key: a memory written under ``X`` is invisible to
every read that does not pass exactly ``X``, and nothing errors when the two
disagree — recall simply returns less. That failure is silent in both
directions, which is why the store accumulated twenty-nine spellings of a dozen
projects.

**The caller should not have to supply it.** ``resolve_tenant`` derives the
tenant from the working directory's git remote, falling back to configuration
and then to ``default``. A location the runtime computes cannot be misspelled by
a caller. This mirrors the decision in ``NousResearch/hermes-agent``, whose
``get_memory_dir()`` resolves a profile-scoped path per call rather than
accepting one — the mechanism is different (a path join there, a partition key
here), the decision is the same.

**An explicit tenant must arrive canonical; it is rejected, never repaired.**
The stance and its cost are the ones ``synapto.scopes`` already documents:
silently rewriting ``Podium/Divergence`` to ``podium/divergence`` hides the
moment two spellings became one key, so the error names the canonical form
instead of applying it.

Two consequences of that stance are worth stating, because they look like
contradictions and are not:

*Derivation normalizes; validation does not.* Lowercasing an owner read out of
a git remote is not repairing a caller's input — GitHub owners are themselves
case-insensitive, and the remote is a machine-read source, not a human's
assertion. A remote that still does not yield a canonical tenant is skipped
rather than raising, because ``resolve_tenant`` runs from arbitrary directories
and an unparseable remote is not an error.

*A rejected spelling and a superseded one are different things.* ``" Podium "``
is not canonical and raises here. ``divergence`` is perfectly canonical and
merely names a tenant that was later folded into another; resolving that is a
lookup in ``tenant_aliases``, which lives above this module because it needs the
database and this does not.

The grammar below deliberately mirrors, but does not import, the scope grammar
in ``synapto.scopes``. A tenant is a storage partition whose stability must not
follow changes to the scope vocabulary; coupling them would let a future scope
type silently repartition every memory in the store.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

DEFAULT_TENANT = "default"

MAX_TENANT_CHARS = 100
TENANT_SEGMENTS = 2

# Anchored with fullmatch() at every call site: with match() and a trailing "$",
# Python accepts a final newline, so "podium\n" would pass as canonical.
_CANONICAL_SEGMENT = re.compile(r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?")
_OWNER_SEGMENT = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
_NAME_SEGMENT = re.compile(r"(?=[a-z0-9._-]*[a-z0-9])[a-z0-9._-]+")

_GRAMMAR_DESCRIPTIONS = {
    _CANONICAL_SEGMENT: "ASCII lowercase letters, digits, and inner '.', '_' or '-'",
    _OWNER_SEGMENT: "an owner must be ASCII lowercase letters, digits, and inner '-'",
    _NAME_SEGMENT: (
        "a name must be ASCII lowercase letters, digits, '.', '_' or '-', "
        "and contain at least one letter or digit"
    ),
}

# owner/name out of the remote spellings git actually produces: scp-style
# (git@host:owner/name.git), and any URL scheme (https://, ssh://, git://).
# A local path has no host segment and matches neither, which is the intent.
_SCP_REMOTE = re.compile(r"^[^/@]+@[^:/]+:(?P<path>.+)$")
_URL_REMOTE = re.compile(r"^[a-z][a-z0-9+.-]*://(?:[^/@]+@)?[^/]+/(?P<path>.+)$", re.IGNORECASE)

CommandRunner = Callable[[Sequence[str]], str]


class InvalidTenantError(ValueError):
    """Raised when a tenant cannot be accepted as written."""


def _matches(pattern: re.Pattern[str], value: str) -> bool:
    """Anchored membership test — fullmatch, never match."""
    return pattern.fullmatch(value) is not None


def _canonical_suggestion(value: str) -> str | None:
    """Return the canonical spelling of ``value``, when one exists.

    Used only to make a rejection actionable. Never applied on the caller's
    behalf — that is the whole point of the stance this module documents.
    """
    candidate = value.strip().lower().replace("\\", "/")
    if candidate and candidate != value and is_canonical_tenant(candidate):
        return candidate
    return None


def is_canonical_tenant(value: str) -> bool:
    """Report whether ``value`` is already a canonical tenant, without raising."""
    if not value or len(value) > MAX_TENANT_CHARS:
        return False

    segments = value.split("/")
    if len(segments) == 1:
        return _matches(_CANONICAL_SEGMENT, segments[0])
    if len(segments) != TENANT_SEGMENTS:
        return False
    owner, name = segments
    return _matches(_OWNER_SEGMENT, owner) and _matches(_NAME_SEGMENT, name)


def validate_tenant(value: object, *, source: str = "tenant") -> str:
    """Return ``value`` unchanged if it is a canonical tenant, else explain why not.

    ``source`` names where the value came from, so a rejection points at the
    thing the reader has to edit — the MCP argument, or the config file.

    Raises:
        InvalidTenantError: the value is not a string, is empty, is longer than
            :data:`MAX_TENANT_CHARS`, or is not canonical.
    """
    if not isinstance(value, str) or isinstance(value, bool):
        raise InvalidTenantError(f"{source} must be a string, got {type(value).__name__}")
    if not value:
        raise InvalidTenantError(f"{source} must not be empty")
    if len(value) > MAX_TENANT_CHARS:
        raise InvalidTenantError(f"{source} exceeds {MAX_TENANT_CHARS} chars (got {len(value)})")

    if is_canonical_tenant(value):
        return value

    suggestion = _canonical_suggestion(value)
    hint = f" — did you mean {suggestion!r}?" if suggestion else ""
    segments = value.split("/")
    if len(segments) > TENANT_SEGMENTS:
        raise InvalidTenantError(
            f"{source} {value!r} has {len(segments)} '/'-separated segments; "
            f"a tenant is either a single name or canonical 'owner/name'{hint}"
        )
    grammar = _GRAMMAR_DESCRIPTIONS[_CANONICAL_SEGMENT if len(segments) == 1 else _NAME_SEGMENT]
    raise InvalidTenantError(
        f"{source} {value!r} is not canonical: {grammar}, and it must arrive already trimmed "
        f"and lowercased{hint}"
    )


def _run_git(command: Sequence[str]) -> str:
    """Read a git remote, treating every failure as "no remote here"."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def _remote_path(remote: str) -> str | None:
    for pattern in (_SCP_REMOTE, _URL_REMOTE):
        found = pattern.match(remote)
        if found:
            return found.group("path")
    return None


def tenant_from_git_remote(cwd: str | None = None, runner: CommandRunner | None = None) -> str | None:
    """Derive ``owner/name`` from the working directory's ``origin`` remote.

    Returns ``None`` — never raises — when there is no repository, no ``origin``,
    or a remote that does not carry an ``owner/name`` pair, such as a plain
    filesystem path. Every one of those is a normal condition for a process
    running outside a checkout, so they fall through to the next source rather
    than failing the caller's write.

    The derived value is lowercased and stripped of a trailing ``.git``. That is
    normalization of a machine-read source, not repair of caller input: git
    remotes and GitHub owners are case-insensitive, so the case a remote happens
    to carry asserts nothing.
    """
    read = runner or _run_git
    remote = read(["git", "-C", cwd or os.getcwd(), "remote", "get-url", "origin"])
    if not remote:
        return None

    path = _remote_path(remote)
    if path is None:
        return None

    path = path.strip("/").removesuffix(".git").lower()
    segments = path.split("/")
    if len(segments) < TENANT_SEGMENTS:
        return None

    candidate = "/".join(segments[-TENANT_SEGMENTS:])
    return candidate if is_canonical_tenant(candidate) else None


_derived_cache: dict[str, str | None] = {}


def clear_tenant_cache() -> None:
    """Forget every derived tenant. Exposed for tests and long-lived processes."""
    _derived_cache.clear()


def _derive_cached(cwd: str, runner: CommandRunner | None) -> str | None:
    """Derive once per working directory.

    The prior art resolves per call because its scope is a path join. Ours
    shells out to git, and ``remember``/``recall`` are hot paths, so the result
    is cached under the directory that produced it. The cache is keyed by
    location rather than held in a module constant, so moving to another
    checkout still re-resolves — which is the property "resolved per call"
    exists to protect. A remote rewritten mid-process is the one case this
    misses; :func:`clear_tenant_cache` is the escape hatch.
    """
    if cwd not in _derived_cache:
        _derived_cache[cwd] = tenant_from_git_remote(cwd, runner)
    return _derived_cache[cwd]


def resolve_tenant(
    explicit: str | None = None,
    *,
    configured: str | None = None,
    cwd: str | None = None,
    runner: CommandRunner | None = None,
) -> str:
    """Resolve the tenant for one operation.

    Order: an explicit override, then the working directory's git remote, then
    configuration, then ``default``. The override comes first because a caller
    who names a tenant is asserting something the location cannot know; it is
    validated, never repaired.

    ``configured`` is validated too, and with its own ``source`` label, so a
    non-canonical ``default_tenant`` names the config file rather than looking
    like the caller's mistake.

    Raises:
        InvalidTenantError: ``explicit`` or ``configured`` is not canonical.
    """
    if explicit is not None:
        return validate_tenant(explicit)

    derived = _derive_cached(cwd or os.getcwd(), runner)
    if derived is not None:
        return derived

    if configured:
        return validate_tenant(configured, source="default_tenant in the synapto config")

    return DEFAULT_TENANT


# -- merge planning ----------------------------------------------------------

EXACT = "exact"
REVIEW = "review"
AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class TenantGroup:
    """One proposed merge, and how much the proposal should be trusted.

    ``canonical`` is ``None`` exactly when the members do not determine one.
    That is not a failure of the planner: two owners claiming the same project
    name is a question only a human can answer, and inventing an answer is how
    a cleanup silently loses memories.
    """

    canonical: str | None
    members: tuple[str, ...]
    confidence: str
    reason: str

    @property
    def is_actionable(self) -> bool:
        return self.canonical is not None and len(self.members) > 1


def _fold(tenant: str) -> str:
    """Collapse the differences that are certainly not meaningful."""
    return tenant.lower().replace("_", "-")


def _name_of(tenant: str) -> str:
    return tenant.split("/")[-1]


def _owner_of(tenant: str) -> str | None:
    segments = tenant.split("/")
    return segments[0] if len(segments) == TENANT_SEGMENTS else None


def _affinity_key(tenant: str, standalone: frozenset[str]) -> str:
    """Group tenants that plausibly name the same project.

    Two shapes are recognised, and only two. A two-segment tenant is keyed by
    its name segment, so ``podium/hermes`` meets ``hermes``. A single-segment
    tenant whose trailing ``-`` component is itself a tenant is keyed by that
    component, so ``podium-kazaam`` meets ``kazaam`` — but ``podium-internal``
    stays itself, because no tenant named ``internal`` exists.

    That second rule is why hyphen splitting can never raise confidence above
    ``review``: a hyphen is a legitimate character in an owner, and nothing in
    the string says which one it is here.
    """
    folded = _fold(tenant)
    if "/" in folded:
        return _name_of(folded)

    _, separator, suffix = folded.rpartition("-")
    if separator and suffix in standalone:
        return suffix
    return folded


def plan_tenant_merges(counts: Mapping[str, int]) -> list[TenantGroup]:
    """Propose how the stored tenants collapse, without deciding anything.

    Deterministic and purely lexical by design. The consolidation lesson this
    project already learned applies here too: grouping by embedding similarity
    would produce a confident-looking mapping nobody can audit, and this
    mapping moves real memories between partitions.

    Groups come back ordered by descending total memory count, so the ones that
    matter are read first. Singletons are included, so the report accounts for
    every tenant in the store rather than only the interesting ones.
    """
    tenants = sorted(counts)
    standalone = frozenset(_fold(t) for t in tenants if "/" not in t)

    buckets: dict[str, list[str]] = {}
    for tenant in tenants:
        buckets.setdefault(_affinity_key(tenant, standalone), []).append(tenant)

    groups = [_classify(members, counts) for members in buckets.values()]
    return sorted(groups, key=lambda g: (-sum(counts[m] for m in g.members), g.members[0]))


def _classify(members: list[str], counts: Mapping[str, int]) -> TenantGroup:
    ordered = tuple(sorted(members, key=lambda t: (-counts[t], t)))
    if len(ordered) == 1:
        return TenantGroup(ordered[0], ordered, EXACT, "only spelling in the store")

    folded = {_fold(t) for t in ordered}
    qualified = [t for t in ordered if "/" in t]
    owners = {_owner_of(t) for t in qualified}

    if len(folded) == 1:
        return TenantGroup(ordered[0], ordered, EXACT, "identical once case and '_' are normalized")

    if len(owners) > 1:
        return TenantGroup(None, ordered, AMBIGUOUS, f"{len(owners)} owners claim this name: {_render(sorted(owners))}")

    if len(qualified) == 1:
        return TenantGroup(qualified[0], ordered, REVIEW, "one 'owner/name' spelling, the rest unqualified")

    return TenantGroup(None, ordered, AMBIGUOUS, "no 'owner/name' spelling to adopt as canonical")


def _render(values: Sequence[str | None]) -> str:
    return ", ".join(v for v in values if v)
