"""Write origin — who authored a memory, recorded at the moment it is written.

A rule the user typed and a rule an automated loop synthesized are
indistinguishable once stored, which is fine while every write is human and
stops being fine the moment an automated writer exists. Any pruning path that
cannot tell them apart will eventually delete something a person authored, and
the audit trail will faithfully record that it did.

Two properties are copied deliberately from ``NousResearch/hermes-agent``, whose
``tools/skill_provenance.py`` treats this as a safety boundary rather than
bookkeeping:

**The writer declares itself, at write time.** ``origin`` is a parameter, not a
derivation. It is never inferred from the transport, the caller, the content, or
where the record lives — their ``skill_usage`` docstring is explicit that
``created_by: agent`` is "an explicit marker ... never inferred from location",
and inference is exactly how such a marker becomes wrong without anyone noticing.

**The default is the conservative one.** Unmarked writes are ``human``. The
asymmetry is the whole argument: labelling an agent's write as human costs a
memory that outlives its usefulness, while labelling a person's write as an
agent's costs a deleted user rule. Only one of those is recoverable.

The vocabulary is closed and small, and adding to it is a migration, because
every destructive path decides what it may touch by reading this value.
"""

from __future__ import annotations

HUMAN = "human"
AGENT = "agent"
CONSOLIDATION = "consolidation"

ORIGINS = (HUMAN, AGENT, CONSOLIDATION)

DEFAULT_ORIGIN = HUMAN

# The origins a destructive pass may act on without being told twice. Human
# writes are excluded by default, and the exclusion is a positive list rather
# than "everything except human" so that adding an origin forces a decision
# about whether it is safe to delete.
AUTOMATED_ORIGINS = (AGENT, CONSOLIDATION)


class InvalidOriginError(ValueError):
    """Raised when an origin is outside the closed vocabulary."""


def validate_origin(value: object, *, source: str = "origin") -> str:
    """Return ``value`` if it names an accepted origin, else explain the choices.

    Raises:
        InvalidOriginError: the value is not a string, or is not one of
            :data:`ORIGINS`.
    """
    if not isinstance(value, str) or isinstance(value, bool):
        raise InvalidOriginError(f"{source} must be a string, got {type(value).__name__}")
    if value not in ORIGINS:
        accepted = ", ".join(ORIGINS)
        raise InvalidOriginError(f"unknown {source} {value!r} — accepted origins are: {accepted}")
    return value


def is_automated(origin: str) -> bool:
    """Report whether a destructive pass may act on this origin unprompted."""
    return origin in AUTOMATED_ORIGINS
