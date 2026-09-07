"""Hybrid search engine — combines vector similarity, full-text, HRR, decay, and depth boosting via RRF.

Ranking formula, in one line::

    score = (rrf_vector + rrf_keyword + rrf_hrr) × decay_score × trust_score × layer_weight

Relevance signals add; quality modifiers multiply the sum. The SQL orders by
``rrf × quality_weight`` to choose the candidates, and Python applies the same
weight after adding the HRR leg, so pre-selection and the final order agree.
Until 0.7.0 the final sort used the raw RRF, which meant decay, trust and the
layer weight decided only *who reached* the candidate list, never the order
the caller saw — ``core`` outranked ``working`` by accident or not at all.

HRR is a third leg on the RRF scale: a candidate gains at most what the
first-ranked row of a SQL leg gains, ``1 / (k + 1)``, in proportion to how far
its similarity clears the noise floor, and nothing below it. Until 0.8.0 it
was added as ``((sim + 1) / 2) × 0.15``: an unrelated memory received +0.075
against a largest possible RRF sum of ≈0.033, so the order inside the window
was decided by the quality weight instead of the match (#103). The probe is
encoded exactly as ``remember`` encodes the memory — content bound to the
content role, extracted entities bound to the entity role — because the old
boost compared an unbound query against a role-bound memory, which is noise by
construction: no golden case ranked its target first on HRR similarity alone.

The layer weights were chosen by measurement (#104, ``docs/eval/layer_sweep.md``),
not by feel. Until 0.8.0 they were ``1.5 / 1.2 / 1.0 / 0.5``, a 3× spread that
let a weaker match in a higher layer outrank the memory that answered the
query. The binding ratio turned out to be ``stable / working``: a lower-layer
memory that restates the query verbatim earns a full HRR leg, so the higher
layer needs about 1.25× to keep the authoritative version first; ``core`` sits
just above ``stable`` because the golden set never asks them to compete, and
``ephemeral`` at 0.7 sinks a note without burying it.

The SQL lives in :mod:`synapto.sql.search` as static templates; nothing here
composes it at runtime. ``DEPTH_BOOST`` mirrors the layer weights the template
spells out, and a test asserts the two agree — agreement by test, not by
generation.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from synapto.db.postgres import PostgresClient
from synapto.embeddings.base import EmbeddingProvider
from synapto.graph.entities import extract_entities_from_text
from synapto.hrr.core import bytes_to_phases, encode_fact, similarity, similarity_noise_floor
from synapto.provenance import DEFAULT_ORIGIN, validate_origin
from synapto.repositories.memories import MemoryRepository
from synapto.repositories.scopes import ScopeRepository
from synapto.scopes import (
    GLOBAL_KEY,
    GLOBAL_TYPE,
    InvalidScopeError,
    ScopeSet,
    reject_conflicting_scope_arguments,
)
from synapto.sql import search as sql

logger = logging.getLogger("synapto.search.hybrid")

DEPTH_BOOST = {
    "core": 1.3,
    "stable": 1.25,
    "working": 1.0,
    "ephemeral": 0.7,
}


@dataclass
class SearchResult:
    id: UUID
    content: str
    summary: str | None
    type: str
    subtype: str | None
    tenant: str
    depth_layer: str
    decay_score: float
    trust_score: float
    rrf_score: float
    metadata: dict[str, Any]
    access_count: int
    created_at: datetime
    accessed_at: datetime
    domain: str | None = None
    scopes: ScopeSet = ScopeSet()
    origin: str = DEFAULT_ORIGIN


DEFAULT_RRF_K = 60


def _hrr_evidence(query: str, hrr_vector: bytes | None, probes: dict[int, Any]) -> float:
    """How strongly one stored HRR vector matches the query, in [0, 1].

    Zero for a row without a vector and for a row whose similarity is within
    :func:`similarity_noise_floor` of zero — both are "no HRR match" — and one
    for an identical vector; linear in the similarity between the two. The probe
    is :func:`encode_fact` of the query, the encoding ``remember`` applied to
    the memory, so like is compared with like. Probes are cached in ``probes``
    by dimension: one search encodes the query once per distinct ``hrr_dim`` in
    the window, not once per row.
    """
    if not hrr_vector:
        return 0.0
    memory_vec = bytes_to_phases(hrr_vector)
    dim = len(memory_vec)
    if dim not in probes:
        probes[dim] = encode_fact(query, extract_entities_from_text(query), dim)
    floor = similarity_noise_floor(dim)
    return max(similarity(probes[dim], memory_vec) - floor, 0.0) / (1.0 - floor)


def _hrr_leg(rows: list[dict[str, Any]], query: str, rrf_k: int) -> list[float]:
    """Contribution of the HRR leg, one entry per row in ``rows`` order, on the RRF scale.

    A candidate gains ``1 / (rrf_k + 1)`` — what the first-ranked row of a SQL
    leg gains — scaled by its :func:`_hrr_evidence`: nothing at the noise floor,
    exactly one leg for a perfect match, linear in between. A candidate without
    a vector or below the floor is outside the leg and gains nothing, as a row
    the full-text predicate rejects gains nothing from the keyword leg.

    The credit follows the similarity rather than the rank because the golden
    set says so: fusing HRR as a third reciprocal-rank leg over the window
    scored 0.877 overall MRR@10 against 0.897 with no HRR at all, since a
    twenty-row window ranks densely and hands a full leg to whichever candidate
    is least weakly related; this form scored 0.921 (#103). O(n·dim) for ``n``
    candidates.
    """
    probes: dict[int, Any] = {}
    return [_hrr_evidence(query, row.get("hrr_vector"), probes) / (rrf_k + 1) for row in rows]


def _rank_candidates(
    rows: list[dict[str, Any]], query: str, limit: int, rrf_k: int = DEFAULT_RRF_K
) -> list[tuple[dict[str, Any], float]]:
    """Order candidates by ``(rrf + rrf_hrr) × quality_weight`` and keep the top ``limit``.

    ``quality_weight`` arrives from the SQL as ``decay × trust × layer_weight``,
    the same product the SQL ordered by to choose the candidates. A row that
    predates the column (a fake in a test, say) weighs 1.0. This is the single
    place the final order is decided; ``hybrid_search`` only feeds it.
    """
    scored = []
    for row, hrr in zip(rows, _hrr_leg(rows, query, rrf_k), strict=True):
        relevance = float(row["rrf_score"]) + hrr
        scored.append((row, relevance * float(row.get("quality_weight", 1.0))))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:limit]


async def _hydrate_scopes(client: PostgresClient, memory_ids: list) -> dict:
    """Attach scopes to a result page in one query.

    Rendering a result without its scopes would leave callers to fetch them per
    hit, which is the N+1 this exists to prevent. Memories with no scopes are
    simply absent from the mapping.
    """
    if not memory_ids:
        return {}
    return await ScopeRepository(client).get_for_memories(memory_ids)


def _build_scope_filter(scopes: ScopeSet | None) -> tuple[str, dict[str, Any]]:
    """Render the applicability predicate and its parameters.

    ``None`` means no scope filter, preserving legacy unfiltered behavior. An
    explicitly empty set is rejected: it can only be a caller mistake, since it
    would match nothing and silently return an empty result.
    """
    if scopes is None:
        return "", {}
    if not scopes:
        raise InvalidScopeError("an empty scope filter matches nothing — omit the filter to search every scope")

    return sql.SCOPE_FILTER, {
        "scope_types": [ref.scope_type for ref in scopes],
        "scope_keys": [ref.scope_key for ref in scopes],
        "global_type": GLOBAL_TYPE,
        "global_key": GLOBAL_KEY,
    }


MAX_METADATA_FILTER_KEYS = 20
MAX_METADATA_FILTER_LIST_ITEMS = 20

_METADATA_SCALARS = (str, int, float, bool, type(None))


class InvalidMetadataFilterError(ValueError):
    """Raised when a metadata filter cannot be read as exact-key equality."""


def validate_metadata_filter(metadata_filter: object) -> dict[str, Any]:
    """Return the filter if every value is a scalar or a list of scalars, else explain why not.

    A scalar value means equality, which is what ``@>`` does for a scalar. A
    list value means "the stored list contains every element", which is also
    exactly what ``@>`` does for an array — and it is the question a facet
    answers ("every memory about ``reasoning-inbox``" against a stored
    ``products`` list). A nested object is still refused: containment on an
    object matches a sub-object, which is not the equality a caller asked for.

    Raises:
        InvalidMetadataFilterError: the filter is not a mapping, is empty, has a
            non-string key, carries more than :data:`MAX_METADATA_FILTER_KEYS`
            entries, nests an object, or carries a list that is empty, longer
            than :data:`MAX_METADATA_FILTER_LIST_ITEMS`, or not all scalars.
    """
    if not isinstance(metadata_filter, Mapping):
        raise InvalidMetadataFilterError(
            f"metadata_filter must be a JSON object of key/value pairs, got {type(metadata_filter).__name__}"
        )
    if not metadata_filter:
        raise InvalidMetadataFilterError(
            "an empty metadata_filter matches every memory — omit the filter instead of passing {}"
        )
    if len(metadata_filter) > MAX_METADATA_FILTER_KEYS:
        raise InvalidMetadataFilterError(
            f"metadata_filter accepts at most {MAX_METADATA_FILTER_KEYS} keys (got {len(metadata_filter)})"
        )

    for key, value in metadata_filter.items():
        if not isinstance(key, str):
            raise InvalidMetadataFilterError(f"metadata_filter keys must be strings, got {type(key).__name__}")
        if isinstance(value, list):
            _validate_list_value(key, value)
        elif not isinstance(value, _METADATA_SCALARS):
            raise InvalidMetadataFilterError(
                f"metadata_filter value for {key!r} is a {type(value).__name__}; only one level of scalar "
                "values, or a list of scalars, is accepted, because containment on a nested object does "
                "not mean exact-key equality"
            )
    return dict(metadata_filter)


def _validate_list_value(key: str, value: list) -> None:
    """Refuse a list that would not mean "contains every element".

    An empty list is contained by every array, so ``{"products": []}`` would
    match every memory that has the key — a filter that filters nothing.
    """
    if not value:
        raise InvalidMetadataFilterError(
            f"metadata_filter list for {key!r} is empty — an empty list is contained by every array, "
            "so it would match every memory that has the key; omit the key instead"
        )
    if len(value) > MAX_METADATA_FILTER_LIST_ITEMS:
        raise InvalidMetadataFilterError(
            f"metadata_filter list for {key!r} accepts at most {MAX_METADATA_FILTER_LIST_ITEMS} elements "
            f"(got {len(value)})"
        )
    for element in value:
        if not isinstance(element, _METADATA_SCALARS):
            raise InvalidMetadataFilterError(
                f"metadata_filter list for {key!r} carries a {type(element).__name__}; list elements must be "
                "scalars, because containment on a nested value does not mean exact-key equality"
            )


def _build_memory_filters(
    *,
    depth_layer: str | None = None,
    subtype: str | None = None,
    domain: str | None = None,
    scopes: ScopeSet | None = None,
    metadata_filter: dict[str, Any] | None = None,
    origin: str | None = None,
    indent: str,
) -> tuple[str, dict[str, Any]]:
    """Build shared optional memory filters.

    Complexity: O(1) time and space because the supported filter set is fixed.
    User values stay in params so SQL rendering remains injection-safe.
    """
    reject_conflicting_scope_arguments(domain, scopes)

    filters: list[str] = []
    params: dict[str, Any] = {}
    if depth_layer:
        filters.append(sql.FILTER_DEPTH_LAYER)
        params["depth_layer"] = depth_layer
    if subtype:
        filters.append(sql.FILTER_SUBTYPE)
        params["subtype"] = subtype
    if domain:
        filters.append(sql.FILTER_DOMAIN)
        params["domain"] = domain

    if origin is not None:
        filters.append(sql.FILTER_ORIGIN)
        params["origin"] = validate_origin(origin)

    if metadata_filter is not None:
        filters.append(sql.FILTER_METADATA)
        params["metadata_filter"] = Jsonb(validate_metadata_filter(metadata_filter))

    scope_sql, scope_params = _build_scope_filter(scopes)
    if scope_sql:
        filters.append(scope_sql)
        params.update(scope_params)
    return f"\n{indent}".join(filters), params


async def hybrid_search(
    client: PostgresClient,
    provider: EmbeddingProvider,
    query: str,
    tenant: str = "default",
    depth_layer: str | None = None,
    subtype: str | None = None,
    limit: int = 10,
    rrf_k: int = DEFAULT_RRF_K,
    *,
    domain: str | None = None,
    scopes: ScopeSet | None = None,
    metadata_filter: dict[str, Any] | None = None,
    origin: str | None = None,
) -> list[SearchResult]:
    """Execute 3-way hybrid RRF search: vector similarity + full-text + HRR.

    The final order is ``(rrf + rrf_hrr) × decay × trust × layer_weight``;
    see the module docstring and :func:`_rank_candidates`. The SQL returns
    ``2 × limit`` candidates so the HRR leg has room to reorder before the cut,
    and ``rrf_k`` is shared by all three legs.

    Filters are built before the query is embedded on purpose: an invalid
    filter must cost zero embedding calls and zero queries, not fail after
    paying for a model round trip.

    ``domain`` and ``scopes`` are keyword-only. ``domain`` had been inserted
    ahead of ``limit``, which silently rebound positional callers' ``limit`` to
    it; putting both after the established positional parameters restores the
    original contract and keeps future filters from repeating the mistake.

    ``scopes=None`` means no applicability filter, preserving legacy behavior.
    """
    filter_sql, filter_params = _build_memory_filters(
        depth_layer=depth_layer,
        subtype=subtype,
        domain=domain,
        scopes=scopes,
        metadata_filter=metadata_filter,
        origin=origin,
        indent="      ",
    )

    embedding = await provider.embed_one(query)
    dim = provider.dimension

    params: dict[str, Any] = {
        "embedding": embedding,
        "query": query,
        "tenant": tenant,
        "rrf_k": rrf_k,
        "limit": limit * 2,
    }
    params.update(filter_params)

    statement = sql.RRF_QUERY_TEMPLATE.format(dim=dim).format(filters=filter_sql)

    rows = await client.execute(statement, params)
    scored_rows = _rank_candidates(rows, query, limit, rrf_k)

    if scored_rows:
        ids = [row["id"] for row, _ in scored_rows]
        await MemoryRepository(client).touch_accessed(ids)
    scopes_by_memory = await _hydrate_scopes(client, [row["id"] for row, _ in scored_rows])

    return [
        SearchResult(
            id=row["id"],
            content=row["content"],
            summary=row["summary"],
            type=row["type"],
            subtype=row.get("subtype"),
            domain=row.get("domain"),
            tenant=row["tenant"],
            depth_layer=row["depth_layer"],
            decay_score=row["decay_score"],
            trust_score=row.get("trust_score", 0.5),
            rrf_score=final_score,
            metadata=row["metadata"] or {},
            origin=row.get("origin", DEFAULT_ORIGIN),
            access_count=row["access_count"],
            created_at=row["created_at"],
            accessed_at=row["accessed_at"],
            scopes=scopes_by_memory.get(row["id"], ScopeSet()),
        )
        for row, final_score in scored_rows
    ]


async def vector_search(
    client: PostgresClient,
    provider: EmbeddingProvider,
    query: str,
    tenant: str = "default",
    depth_layer: str | None = None,
    subtype: str | None = None,
    limit: int = 10,
    *,
    domain: str | None = None,
    scopes: ScopeSet | None = None,
    metadata_filter: dict[str, Any] | None = None,
    origin: str | None = None,
) -> list[SearchResult]:
    """Pure vector similarity search (no keyword component).

    ``domain`` and ``scopes`` are keyword-only, for the reason documented on
    :func:`hybrid_search`.
    """
    filter_sql, filter_params = _build_memory_filters(
        depth_layer=depth_layer,
        subtype=subtype,
        domain=domain,
        scopes=scopes,
        metadata_filter=metadata_filter,
        origin=origin,
        indent="  ",
    )

    embedding = await provider.embed_one(query)
    dim = provider.dimension

    params: dict[str, Any] = {
        "embedding": embedding,
        "tenant": tenant,
        "limit": limit,
    }
    params.update(filter_params)

    statement = sql.VECTOR_ONLY_TEMPLATE.format(dim=dim).format(filters=filter_sql)

    rows = await client.execute(statement, params)
    scopes_by_memory = await _hydrate_scopes(client, [row["id"] for row in rows])

    return [
        SearchResult(
            id=row["id"],
            content=row["content"],
            summary=row["summary"],
            type=row["type"],
            subtype=row.get("subtype"),
            domain=row.get("domain"),
            scopes=scopes_by_memory.get(row["id"], ScopeSet()),
            tenant=row["tenant"],
            depth_layer=row["depth_layer"],
            decay_score=row["decay_score"],
            trust_score=row.get("trust_score", 0.5),
            rrf_score=row.get("similarity", 0.0),
            metadata=row["metadata"] or {},
            origin=row.get("origin", DEFAULT_ORIGIN),
            access_count=row["access_count"],
            created_at=row["created_at"],
            accessed_at=row["accessed_at"],
        )
        for row in rows
    ]


async def count_memories(
    client: PostgresClient,
    *,
    tenant: str = "default",
    depth_layer: str | None = None,
    subtype: str | None = None,
    domain: str | None = None,
    scopes: ScopeSet | None = None,
    metadata_filter: dict[str, Any] | None = None,
    origin: str | None = None,
) -> int:
    """Count every memory matching the filters, independent of any page size.

    Deliberately not a variant of :func:`hybrid_search`. A hybrid result is a
    relevance-ranked page whose candidate set comes from vector and full-text
    similarity, so "how many did that match" is not a well-defined number.
    Aggregation asks a different question — how many memories carry this exact
    key — and that one has an exact answer.

    It shares :func:`_build_memory_filters` with the search rather than
    restating the predicates, so a count and a page can never disagree about
    what "matching" means. That was the whole failure being replaced: a
    threshold computed from a page is a lower bound that stops being one as the
    store grows, while looking like a count the entire time.

    Complexity: one indexed aggregate. The GIN index added in migration 008
    serves the containment predicate.
    """
    filter_sql, filter_params = _build_memory_filters(
        depth_layer=depth_layer,
        subtype=subtype,
        domain=domain,
        scopes=scopes,
        metadata_filter=metadata_filter,
        origin=origin,
        indent="  ",
    )
    params = {"tenant": tenant, **filter_params}
    row = await client.execute_one(sql.COUNT.format(filters=filter_sql), params)
    return int(row["total"]) if row else 0
