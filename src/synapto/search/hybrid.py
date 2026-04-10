"""Hybrid search engine — combines vector similarity, full-text, HRR, decay, and depth boosting via RRF."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from synapto.db.postgres import PostgresClient
from synapto.embeddings.base import EmbeddingProvider

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
# semantic + keyword, then Python adds the HRR signal.
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
      {{depth_filter}}
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
      {{depth_filter}}
    ORDER BY ts_rank_cd(tsv, plainto_tsquery('english', %(query)s)) DESC
    LIMIT 20
)
SELECT
    m.id,
    m.content,
    m.summary,
    m.type,
    m.tenant,
    m.depth_layer,
    m.decay_score,
    m.trust_score,
    m.metadata,
    m.access_count,
    m.created_at,
    m.accessed_at,
    m.hrr_vector,
    COALESCE(1.0 / (%(rrf_k)s + s.rank), 0.0) +
    COALESCE(1.0 / (%(rrf_k)s + k.rank), 0.0) AS rrf_score
FROM memories m
LEFT JOIN semantic_search s ON m.id = s.id
LEFT JOIN keyword_search k ON m.id = k.id
WHERE (s.id IS NOT NULL OR k.id IS NOT NULL)
ORDER BY
    (COALESCE(1.0 / (%(rrf_k)s + s.rank), 0.0) +
     COALESCE(1.0 / (%(rrf_k)s + k.rank), 0.0)) *
    m.decay_score *
    m.trust_score *
    CASE m.depth_layer
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
    tenant: str
    depth_layer: str
    decay_score: float
    trust_score: float
    rrf_score: float
    metadata: dict[str, Any]


def _compute_hrr_boost(query: str, hrr_vector: bytes | None, hrr_weight: float = 0.15) -> float:
    """Compute HRR similarity boost for a candidate memory.

    Returns a value in [0, hrr_weight] that gets added to the RRF score.
    Gracefully returns 0.0 if hrr_vector is None (backward compat).
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


async def hybrid_search(
    client: PostgresClient,
    provider: EmbeddingProvider,
    query: str,
    tenant: str = "default",
    depth_layer: str | None = None,
    limit: int = 10,
    rrf_k: int = 60,
) -> list[SearchResult]:
    """Execute 3-way hybrid RRF search: vector similarity + full-text + HRR."""
    embedding = await provider.embed_one(query)
    dim = provider.dimension

    depth_filter = ""
    if depth_layer:
        depth_filter = f"AND depth_layer = '{depth_layer}'"

    sql = RRF_QUERY_TEMPLATE.format(dim=dim).format(depth_filter=depth_filter)

    rows = await client.execute(sql, {
        "embedding": embedding,
        "query": query,
        "tenant": tenant,
        "rrf_k": rrf_k,
        "limit": limit * 2,  # fetch extra for HRR reranking
    })

    # apply HRR boost and rerank
    scored_rows = []
    for row in rows:
        hrr_boost = _compute_hrr_boost(query, row.get("hrr_vector"))
        final_score = float(row["rrf_score"]) + hrr_boost
        scored_rows.append((row, final_score))

    scored_rows.sort(key=lambda x: x[1], reverse=True)
    scored_rows = scored_rows[:limit]

    if scored_rows:
        ids = [row["id"] for row, _ in scored_rows]
        await client.execute(
            "UPDATE memories SET accessed_at = now(), access_count = access_count + 1 "
            "WHERE id = ANY(%s);",
            (ids,),
        )

    return [
        SearchResult(
            id=row["id"],
            content=row["content"],
            summary=row["summary"],
            type=row["type"],
            tenant=row["tenant"],
            depth_layer=row["depth_layer"],
            decay_score=row["decay_score"],
            trust_score=row.get("trust_score", 0.5),
            rrf_score=final_score,
            metadata=row["metadata"] or {},
        )
        for row, final_score in scored_rows
    ]


VECTOR_ONLY_TEMPLATE = """
SELECT
    id, content, summary, type, tenant, depth_layer, decay_score, trust_score, metadata,
    access_count, created_at, accessed_at,
    1 - (embedding::vector({dim}) <=> %(embedding)s::vector({dim})) AS similarity
FROM memories
WHERE deleted_at IS NULL
  AND tenant = %(tenant)s
  {{depth_filter}}
ORDER BY embedding::vector({dim}) <=> %(embedding)s::vector({dim})
LIMIT %(limit)s;
"""


async def vector_search(
    client: PostgresClient,
    provider: EmbeddingProvider,
    query: str,
    tenant: str = "default",
    depth_layer: str | None = None,
    limit: int = 10,
) -> list[SearchResult]:
    """Pure vector similarity search (no keyword component)."""
    embedding = await provider.embed_one(query)
    dim = provider.dimension

    depth_filter = ""
    if depth_layer:
        depth_filter = f"AND depth_layer = '{depth_layer}'"

    sql = VECTOR_ONLY_TEMPLATE.format(dim=dim).format(depth_filter=depth_filter)

    rows = await client.execute(sql, {
        "embedding": embedding,
        "tenant": tenant,
        "limit": limit,
    })

    return [
        SearchResult(
            id=row["id"],
            content=row["content"],
            summary=row["summary"],
            type=row["type"],
            tenant=row["tenant"],
            depth_layer=row["depth_layer"],
            decay_score=row["decay_score"],
            trust_score=row.get("trust_score", 0.5),
            rrf_score=row.get("similarity", 0.0),
            metadata=row["metadata"] or {},
        )
        for row in rows
    ]
