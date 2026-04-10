"""HRR-based compositional retrieval for Synapto.

Provides algebraic memory queries that go beyond keyword and vector similarity:
- probe: find memories where an entity plays a structural role
- reason: multi-entity compositional query (vector-space JOIN)
- contradict: detect contradictory memories via entity overlap + content divergence

Design pattern: Strategy -- each retrieval method (probe, reason, contradict)
implements a different scoring strategy over the same HRR vector space. The caller
selects the strategy based on query intent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from synapto.db.postgres import PostgresClient
from synapto.hrr.core import (
    DEFAULT_DIM,
    bind,
    bytes_to_phases,
    encode_atom,
    encode_text,
    similarity,
    unbind,
)

logger = logging.getLogger("synapto.hrr.retrieval")


@dataclass
class HRRResult:
    """A memory scored by HRR compositional similarity."""

    id: UUID
    content: str
    type: str
    tenant: str
    depth_layer: str
    trust_score: float
    hrr_score: float


async def _fetch_hrr_memories(
    client: PostgresClient,
    tenant: str,
    depth_layer: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Fetch memories that have HRR vectors."""
    where = ["deleted_at IS NULL", "tenant = %s", "hrr_vector IS NOT NULL"]
    params: list = [tenant]
    if depth_layer:
        where.append("depth_layer = %s")
        params.append(depth_layer)

    return await client.execute(
        f"""
        SELECT id, content, type, tenant, depth_layer, trust_score, hrr_vector
        FROM memories
        WHERE {' AND '.join(where)}
        LIMIT %s;
        """,
        (*params, limit),
    )


def _score_to_unit(sim: float) -> float:
    """Map similarity from [-1, 1] to [0, 1]."""
    return (sim + 1.0) / 2.0


async def probe(
    client: PostgresClient,
    entity: str,
    tenant: str = "default",
    depth_layer: str | None = None,
    limit: int = 10,
    dim: int = DEFAULT_DIM,
) -> list[HRRResult]:
    """Find memories where an entity plays a structural role.

    Unbinds entity+ROLE_ENTITY from each memory's HRR vector and measures
    how well the residual matches the content signal. This is algebraic
    structure matching, not keyword search.
    """
    role_entity = encode_atom("__hrr_role_entity__", dim)
    role_content = encode_atom("__hrr_role_content__", dim)
    entity_vec = encode_atom(entity.lower(), dim)
    probe_key = bind(entity_vec, role_entity)

    rows = await _fetch_hrr_memories(client, tenant, depth_layer, limit=500)
    if not rows:
        return []

    scored = []
    for row in rows:
        fact_vec = bytes_to_phases(row["hrr_vector"])
        residual = unbind(fact_vec, probe_key)
        content_vec = bind(encode_text(row["content"], dim), role_content)
        sim = similarity(residual, content_vec)
        score = _score_to_unit(sim) * row["trust_score"]
        scored.append((row, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [
        HRRResult(
            id=row["id"],
            content=row["content"],
            type=row["type"],
            tenant=row["tenant"],
            depth_layer=row["depth_layer"],
            trust_score=row["trust_score"],
            hrr_score=score,
        )
        for row, score in scored[:limit]
    ]


async def reason(
    client: PostgresClient,
    entities: list[str],
    tenant: str = "default",
    depth_layer: str | None = None,
    limit: int = 10,
    dim: int = DEFAULT_DIM,
) -> list[HRRResult]:
    """Multi-entity compositional query -- vector-space JOIN.

    Finds memories related to ALL given entities simultaneously by taking the
    minimum structural similarity across all entities (AND semantics).
    No embedding database can do this -- it requires algebraic structure.
    """
    if not entities:
        return []

    role_entity = encode_atom("__hrr_role_entity__", dim)
    role_content = encode_atom("__hrr_role_content__", dim)

    probe_keys = [
        bind(encode_atom(e.lower(), dim), role_entity)
        for e in entities
    ]

    rows = await _fetch_hrr_memories(client, tenant, depth_layer, limit=500)
    if not rows:
        return []

    scored = []
    for row in rows:
        fact_vec = bytes_to_phases(row["hrr_vector"])
        entity_scores = []
        for probe_key in probe_keys:
            residual = unbind(fact_vec, probe_key)
            sim = similarity(residual, role_content)
            entity_scores.append(sim)

        # AND semantics: score is limited by the weakest entity match
        min_sim = min(entity_scores)
        score = _score_to_unit(min_sim) * row["trust_score"]
        scored.append((row, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [
        HRRResult(
            id=row["id"],
            content=row["content"],
            type=row["type"],
            tenant=row["tenant"],
            depth_layer=row["depth_layer"],
            trust_score=row["trust_score"],
            hrr_score=score,
        )
        for row, score in scored[:limit]
    ]


@dataclass
class Contradiction:
    """A pair of potentially contradictory memories."""

    memory_a: HRRResult
    memory_b: HRRResult
    shared_entities: list[str]
    entity_overlap: float
    content_similarity: float
    contradiction_score: float


async def contradict(
    client: PostgresClient,
    tenant: str = "default",
    threshold: float = 0.3,
    limit: int = 10,
    dim: int = DEFAULT_DIM,
) -> list[Contradiction]:
    """Detect contradictory memories via entity overlap + low HRR content similarity.

    Two memories contradict when they share entities (same subject) but have
    divergent content vectors (different claims). This is automated memory hygiene.
    """
    # fetch memories with HRR vectors
    rows = await _fetch_hrr_memories(client, tenant, limit=500)
    if len(rows) < 2:
        return []

    # build entity sets per memory via DB query - O(n) queries
    # using frozenset for O(1) intersection/union operations
    memory_entities: dict[UUID, frozenset[str]] = {}
    for row in rows:
        ent_rows = await client.execute(
            """
            SELECT e.name FROM entities e
            JOIN memory_entities me ON me.entity_id = e.id
            WHERE me.memory_id = %s;
            """,
            (row["id"],),
        )
        memory_entities[row["id"]] = frozenset(r["name"].lower() for r in ent_rows)

    contradictions = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            r1, r2 = rows[i], rows[j]
            ents1 = memory_entities.get(r1["id"], frozenset())
            ents2 = memory_entities.get(r2["id"], frozenset())

            if not ents1 or not ents2:
                continue

            # O(1) set operations on frozensets
            intersection = ents1 & ents2
            union = ents1 | ents2
            entity_overlap = len(intersection) / len(union) if union else 0.0

            if entity_overlap < 0.3:
                continue

            v1 = bytes_to_phases(r1["hrr_vector"])
            v2 = bytes_to_phases(r2["hrr_vector"])
            content_sim = similarity(v1, v2)

            # high entity overlap + low content similarity = contradiction
            contradiction_score = entity_overlap * (1.0 - _score_to_unit(content_sim))

            if contradiction_score >= threshold:
                contradictions.append(Contradiction(
                    memory_a=HRRResult(
                        id=r1["id"], content=r1["content"], type=r1["type"],
                        tenant=r1["tenant"], depth_layer=r1["depth_layer"],
                        trust_score=r1["trust_score"], hrr_score=0.0,
                    ),
                    memory_b=HRRResult(
                        id=r2["id"], content=r2["content"], type=r2["type"],
                        tenant=r2["tenant"], depth_layer=r2["depth_layer"],
                        trust_score=r2["trust_score"], hrr_score=0.0,
                    ),
                    shared_entities=sorted(intersection),
                    entity_overlap=entity_overlap,
                    content_similarity=content_sim,
                    contradiction_score=contradiction_score,
                ))

    contradictions.sort(key=lambda x: x.contradiction_score, reverse=True)
    return contradictions[:limit]
