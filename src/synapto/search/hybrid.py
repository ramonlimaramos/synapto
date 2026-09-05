"""Hybrid search engine — combines vector similarity, full-text, HRR, decay, and depth boosting via RRF.

Ranking formula, in one line::

    score = (rrf_vector + rrf_keyword + hrr_boost) × decay_score × trust_score × layer_weight

Relevance signals add; quality modifiers multiply the sum. The SQL orders by
``rrf × quality_weight`` to choose the candidates, and Python applies the same
weight after adding the HRR boost, so pre-selection and the final order agree.
Until 0.7.0 the final sort used the raw RRF, which meant decay, trust and the
layer weight decided only *who reached* the candidate list, never the order
the caller saw — ``core`` outranked ``working`` by accident or not at all.

The SQL is a static template; nothing composes it at runtime. ``DEPTH_BOOST``
mirrors the layer weights the template spells out, and a test asserts the two
agree — agreement by test, not by generation.
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

logger = logging.getLogger("synapto.search.hybrid")

DEPTH_BOOST = {
    "core": 1.5,
    "stable": 1.2,
    "working": 1.0,
    "ephemeral": 0.5,
}


# 3-way RRF: semantic + keyword + HRR scoring.
# HRR scoring is done client-side (Python) because it uses bytea vectors
# that PostgreSQL cannot natively rank. The SQL query fetches candidates from
# semantic + keyword ordered by rrf × quality_weight, then Python adds the HRR
# signal and applies the same weight (see the module docstring).
#
# NOTE: {dim} is injected via str.format() (safe — always an int from provider.dimension).
# Query params use %(name)s placeholders for psycopg.
RRF_QUERY_TEMPLATE = """
WITH semantic_search AS (
    SELECT
        id,
        RANK() OVER (ORDER BY embedding::vector({dim}) <=> %(embedding)s::vector({dim})) AS rank
    FROM memories
    WHERE deleted_at IS NULL
      AND tenant = %(tenant)s
      {{filters}}
    ORDER BY embedding::vector({dim}) <=> %(embedding)s::vector({dim})
    LIMIT 20
),
keyword_search AS (
    SELECT
        id,
        RANK() OVER (
            ORDER BY ts_rank_cd(tsv, plainto_tsquery('english', %(query)s)) DESC
        ) AS rank
    FROM memories
    WHERE deleted_at IS NULL
      AND tenant = %(tenant)s
      AND tsv @@ plainto_tsquery('english', %(query)s)
      {{filters}}
    ORDER BY ts_rank_cd(tsv, plainto_tsquery('english', %(query)s)) DESC
    LIMIT 20
)
SELECT
    m.id,
    m.content,
    m.summary,
    m.type,
    m.subtype,
    m.domain,
    m.tenant,
    m.depth_layer,
    m.decay_score,
    m.trust_score,
    m.metadata,
    m.origin,
    m.access_count,
    m.created_at,
    m.accessed_at,
    m.hrr_vector,
    COALESCE(1.0 / (%(rrf_k)s + s.rank), 0.0) +
    COALESCE(1.0 / (%(rrf_k)s + k.rank), 0.0) AS rrf_score,
    m.decay_score * m.trust_score * CASE m.depth_layer
        WHEN 'core' THEN 1.5
        WHEN 'stable' THEN 1.2
        WHEN 'working' THEN 1.0
        WHEN 'ephemeral' THEN 0.5
        ELSE 1.0
    END AS quality_weight
FROM memories m
LEFT JOIN semantic_search s ON m.id = s.id
LEFT JOIN keyword_search k ON m.id = k.id
WHERE (s.id IS NOT NULL OR k.id IS NOT NULL)
ORDER BY
    (COALESCE(1.0 / (%(rrf_k)s + s.rank), 0.0) +
     COALESCE(1.0 / (%(rrf_k)s + k.rank), 0.0)) *
    m.decay_score * m.trust_score * CASE m.depth_layer
        WHEN 'core' THEN 1.5
        WHEN 'stable' THEN 1.2
        WHEN 'working' THEN 1.0
        WHEN 'ephemeral' THEN 0.5
        ELSE 1.0
    END DESC
LIMIT %(limit)s;
"""


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


def _compute_hrr_boost(query: str, hrr_vector: bytes | None, hrr_weight: float = 0.15) -> float:
    """Compute HRR similarity boost for a candidate memory.

    Returns a value in [0, hrr_weight] that is added to the RRF score before
    the quality weight is applied. Gracefully returns 0.0 if hrr_vector is
    None (backward compat).
    """
    if not hrr_vector:
        return 0.0
    try:
        from synapto.hrr.core import bytes_to_phases, encode_text, similarity

        query_vec = encode_text(query)
        memory_vec = bytes_to_phases(hrr_vector)
        sim = similarity(query_vec, memory_vec)
        # map [-1, 1] to [0, hrr_weight]
        return ((sim + 1.0) / 2.0) * hrr_weight
    except Exception:
        return 0.0


def _rank_candidates(rows: list[dict[str, Any]], query: str, limit: int) -> list[tuple[dict[str, Any], float]]:
    """Order candidates by ``(rrf + hrr) × quality_weight`` and keep the top ``limit``.

    ``quality_weight`` arrives from the SQL as ``decay × trust × layer_weight``,
    the same product the SQL ordered by to choose the candidates. A row that
    predates the column (a fake in a test, say) weighs 1.0. This is the single
    place the final order is decided; ``hybrid_search`` only feeds it.
    """
    scored = []
    for row in rows:
        relevance = float(row["rrf_score"]) + _compute_hrr_boost(query, row.get("hrr_vector"))
        scored.append((row, relevance * float(row.get("quality_weight", 1.0))))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:limit]


# Applicability, as one correlated predicate so no join fans out and RRF ranking
# stays intact. Reading it inside-out:
#   * the memory must have at least one scope, which is what excludes unscoped
#     legacy rows whenever a filter is requested;
#   * global:all short-circuits to a match;
#   * otherwise every scope type the memory carries must be satisfied by at
#     least one requested value of that type. Grouping by scope_type gives OR
#     within a type for free, and NOT EXISTS over the unsatisfied groups gives
#     AND across types. A requested type the memory does not carry imposes
#     nothing, which is the "extra types in the query" rule.
# The q side is COLLATE "C" to match the columns' byte-oriented collation, so
# identity here is the same identity the primary key uses.
_SCOPE_FILTER = """AND EXISTS (
          SELECT 1 FROM memory_scopes any_scope
          WHERE any_scope.memory_id = memories.id
      )
      AND (
          EXISTS (
              SELECT 1 FROM memory_scopes global_scope
              WHERE global_scope.memory_id = memories.id
                AND global_scope.scope_type = %(global_type)s
                AND global_scope.scope_key = %(global_key)s
          )
          OR NOT EXISTS (
              SELECT 1
              FROM memory_scopes ms
              LEFT JOIN unnest(%(scope_types)s::text[], %(scope_keys)s::text[])
                     AS q(scope_type, scope_key)
                ON q.scope_type COLLATE "C" = ms.scope_type
               AND q.scope_key COLLATE "C" = ms.scope_key
              WHERE ms.memory_id = memories.id
              GROUP BY ms.scope_type
              HAVING count(q.scope_key) = 0
          )
      )"""


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

    return _SCOPE_FILTER, {
        "scope_types": [ref.scope_type for ref in scopes],
        "scope_keys": [ref.scope_key for ref in scopes],
        "global_type": GLOBAL_TYPE,
        "global_key": GLOBAL_KEY,
    }


MAX_METADATA_FILTER_KEYS = 20

# Containment reads a flat mapping of scalars. Nesting is refused rather than
# passed through: `@>` on a nested object matches sub-objects and treats arrays
# as subsets, so `{"a": {"b": 1}}` and `{"tags": ["x"]}` mean something subtler
# than the exact-key equality this filter exists to provide, and a caller would
# have to know which. One level of scalars has exactly one reading.
_METADATA_SCALARS = (str, int, float, bool, type(None))


class InvalidMetadataFilterError(ValueError):
    """Raised when a metadata filter cannot be read as exact-key equality."""


def validate_metadata_filter(metadata_filter: object) -> dict[str, Any]:
    """Return the filter if it is a flat mapping of scalars, else explain why not.

    Raises:
        InvalidMetadataFilterError: the filter is not a mapping, is empty, has a
            non-string key, carries more than :data:`MAX_METADATA_FILTER_KEYS`
            entries, or nests a value.
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
        if not isinstance(value, _METADATA_SCALARS):
            raise InvalidMetadataFilterError(
                f"metadata_filter value for {key!r} is a {type(value).__name__}; only one level of scalar "
                "values is accepted, because containment on nested objects and arrays does not mean "
                "exact-key equality"
            )
    return dict(metadata_filter)


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
        filters.append("AND depth_layer = %(depth_layer)s")
        params["depth_layer"] = depth_layer
    if subtype:
        filters.append("AND subtype = %(subtype)s")
        params["subtype"] = subtype
    if domain:
        filters.append("AND domain = %(domain)s")
        params["domain"] = domain

    if origin is not None:
        filters.append("AND origin = %(origin)s")
        params["origin"] = validate_origin(origin)

    if metadata_filter is not None:
        filters.append("AND metadata @> %(metadata_filter)s::jsonb")
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
    rrf_k: int = 60,
    *,
    domain: str | None = None,
    scopes: ScopeSet | None = None,
    metadata_filter: dict[str, Any] | None = None,
    origin: str | None = None,
) -> list[SearchResult]:
    """Execute 3-way hybrid RRF search: vector similarity + full-text + HRR.

    The final order is ``(rrf + hrr_boost) × decay × trust × layer_weight``;
    see the module docstring and :func:`_rank_candidates`. The SQL returns
    ``2 × limit`` candidates so the HRR boost has room to reorder before the cut.

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

    sql = RRF_QUERY_TEMPLATE.format(dim=dim).format(filters=filter_sql)

    rows = await client.execute(sql, params)
    scored_rows = _rank_candidates(rows, query, limit)

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


VECTOR_ONLY_TEMPLATE = """
SELECT
    id, content, summary, type, subtype, domain, tenant, depth_layer, decay_score, trust_score, metadata,
    origin,
    access_count, created_at, accessed_at,
    1 - (embedding::vector({dim}) <=> %(embedding)s::vector({dim})) AS similarity
FROM memories
WHERE deleted_at IS NULL
  AND tenant = %(tenant)s
  {{filters}}
ORDER BY embedding::vector({dim}) <=> %(embedding)s::vector({dim})
LIMIT %(limit)s;
"""


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

    sql = VECTOR_ONLY_TEMPLATE.format(dim=dim).format(filters=filter_sql)

    rows = await client.execute(sql, params)
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


_COUNT_QUERY = """
SELECT count(*) AS total
FROM memories
WHERE deleted_at IS NULL
  AND tenant = %(tenant)s
  {filters}
"""


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
    row = await client.execute_one(_COUNT_QUERY.format(filters=filter_sql), params)
    return int(row["total"]) if row else 0
