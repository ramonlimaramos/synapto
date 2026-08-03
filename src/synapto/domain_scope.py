"""Domain scope value object.

A domain is the skill/repo/language bounded context a memory belongs to
(``python``, ``synapto``, ``jerry-workday``). Unlike free-form content, it is a
*query axis*: a memory is only retrievable by domain when the value written and
the value queried are byte-identical.

Canonicalization therefore cannot live in any single adapter. It lives here, and
every surface that writes or filters a domain — MCP tools, CLI, repositories,
search — routes through these functions, so a new caller cannot reintroduce the
"stored ``Python``, queried ``python``, found nothing" class of bug.
"""

MAX_DOMAIN_CHARS = 50


class InvalidDomainError(ValueError):
    """Raised when a domain scope cannot be canonicalized to a storable value."""


def normalize_domain(domain: str | None) -> str | None:
    """Canonicalize a domain for the write path.

    Trims boundary whitespace and lowercases, so that every spelling of a domain
    maps to one storage key. ``None`` passes through (the memory simply has no
    domain), but an empty or whitespace-only string is an error: it would persist
    as a non-NULL value that the truthiness-gated filter and the output
    formatting can never match.

    Length is checked *after* normalization, since that is what reaches the
    ``VARCHAR(50)`` column.

    Raises:
        InvalidDomainError: the value is blank or too long once normalized.
    """
    if domain is None:
        return None

    normalized = domain.strip().lower()

    if not normalized:
        raise InvalidDomainError("domain must not be empty or whitespace-only — omit it instead")
    if len(normalized) > MAX_DOMAIN_CHARS:
        raise InvalidDomainError(f"domain exceeds {MAX_DOMAIN_CHARS} chars (got {len(normalized)})")
    return normalized


def normalize_domain_filter(domain: str | None) -> str | None:
    """Canonicalize a domain for the read path.

    Identical to :func:`normalize_domain`, except that a blank value means "no
    domain filter" rather than an error — callers express an absent filter as
    ``None``, and a CLI flag defaulting to an empty string should not raise.
    An over-long filter is still rejected, since it can only be a caller mistake.

    Raises:
        InvalidDomainError: the value is too long once normalized.
    """
    if domain is None or not domain.strip():
        return None
    return normalize_domain(domain)
