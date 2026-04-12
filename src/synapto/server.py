"""Synapto MCP server — exposes memory graph tools via the Model Context Protocol."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastmcp import FastMCP

from synapto.config import load_config
from synapto.db.migrations import ensure_hnsw_index, run_migrations
from synapto.db.postgres import PostgresClient
from synapto.db.redis_cache import RedisCache
from synapto.decay.maintenance import cleanup_ephemeral, update_decay_scores
from synapto.embeddings.base import EmbeddingProvider
from synapto.embeddings.registry import get_provider
from synapto.graph.entities import create_entity, extract_entities_from_text
from synapto.graph.relations import create_relation_by_name
from synapto.hrr.banks import rebuild_bank
from synapto.hrr.core import DEFAULT_DIM, encode_fact, phases_to_bytes
from synapto.hrr.retrieval import contradict as hrr_contradict
from synapto.repositories.entities import EntityRepository
from synapto.repositories.memories import MemoryRepository
from synapto.repositories.relations import RelationRepository
from synapto.search.graph import traverse
from synapto.search.hybrid import hybrid_search

logger = logging.getLogger("synapto.server")

# --- globals initialized on startup ---
_pg: PostgresClient | None = None
_cache: RedisCache | None = None
_provider: EmbeddingProvider | None = None
_config = None


def _get_pg() -> PostgresClient:
    if _pg is None:
        raise RuntimeError("synapto server not initialized")
    return _pg


def _get_cache() -> RedisCache:
    if _cache is None:
        raise RuntimeError("synapto redis not initialized")
    return _cache


def _get_provider() -> EmbeddingProvider:
    if _provider is None:
        raise RuntimeError("synapto embedding provider not initialized")
    return _provider


@asynccontextmanager
async def _lifespan(server):
    """Startup/shutdown lifecycle for the Synapto MCP server."""
    global _pg, _cache, _provider, _config

    _config = load_config()
    logger.info("synapto config loaded (pg=%s, redis=%s)", _config.pg_dsn, _config.redis_url)

    _pg = PostgresClient(_config.pg_dsn)
    await _pg.connect()
    await run_migrations(_pg)

    _cache = RedisCache(_config.redis_url)
    await _cache.connect()

    kwargs = {}
    if _config.embedding_model:
        kwargs["model_name"] = _config.embedding_model
    _provider = get_provider(_config.embedding_provider, **kwargs)

    await ensure_hnsw_index(_pg, _provider.dimension)
    logger.info("synapto MCP server ready (provider=%s, dim=%d)", _provider.name, _provider.dimension)

    try:
        yield
    finally:
        if _cache:
            await _cache.close()
        if _pg:
            await _pg.close()
        logger.info("synapto MCP server shut down")


mcp = FastMCP("synapto", lifespan=_lifespan)


@mcp.tool
async def remember(
    content: str,
    memory_type: str = "general",
    tenant: str | None = None,
    depth_layer: str = "working",
    summary: str | None = None,
    metadata: dict[str, Any] | None = None,
    extract_entities: bool = True,
) -> str:
    """Store a memory with optional entity extraction.

    Args:
        content: the memory content to store
        memory_type: category (general, user, feedback, project, reference)
        tenant: project/tenant scope (defaults to config default)
        depth_layer: core, stable, working, or ephemeral
        summary: optional short summary
        metadata: optional JSON metadata
        extract_entities: auto-extract and link entities from content
    """
    pg = _get_pg()
    provider = _get_provider()
    t = tenant or _config.default_tenant
    repo = MemoryRepository(pg)

    embedding = await provider.embed_one(content)
    memory_id = await repo.create(
        content=content,
        embedding=embedding,
        embedding_dim=provider.dimension,
        memory_type=memory_type,
        tenant=t,
        depth_layer=depth_layer,
        summary=summary,
        metadata=metadata,
    )

    entity_names = []
    if extract_entities:
        entity_names = extract_entities_from_text(content)
        for entity_name in entity_names:
            eid = await create_entity(pg, entity_name, "concept", t, provider=provider)
            await EntityRepository(pg).link_memory(memory_id, eid)

    try:
        hrr_vec = encode_fact(content, entity_names)
        await repo.update_hrr(memory_id, phases_to_bytes(hrr_vec), DEFAULT_DIM)
        bank_name = f"{t}:{memory_type}"
        await rebuild_bank(pg, bank_name, t, type_filter=memory_type)
    except Exception as e:
        logger.warning("hrr vector computation failed for %s: %s", memory_id, e)

    cache = _get_cache()
    await cache.cache_memory(memory_id, {"content": content, "type": memory_type, "tenant": t})

    entity_count = len(entity_names)
    return f"stored memory {memory_id} ({depth_layer}, {entity_count} entities linked)"


@mcp.tool
async def recall(
    query: str,
    tenant: str | None = None,
    depth_layer: str | None = None,
    limit: int = 10,
) -> str:
    """Search memories using hybrid semantic + keyword search with RRF ranking.

    Args:
        query: natural language search query
        tenant: filter to a specific project/tenant
        depth_layer: filter to a specific depth layer (core, stable, working, ephemeral)
        limit: max results to return
    """
    pg = _get_pg()
    provider = _get_provider()
    t = tenant or _config.default_tenant

    results = await hybrid_search(pg, provider, query, tenant=t, depth_layer=depth_layer, limit=limit)

    if not results:
        return "no memories found matching your query"

    output = []
    for r in results:
        output.append(
            f"[{r.depth_layer}] ({r.type}) score={r.rrf_score:.4f} "
            f"decay={r.decay_score:.2f} trust={r.trust_score:.2f}\n"
            f"  {r.content[:200]}{'...' if len(r.content) > 200 else ''}\n"
            f"  id={r.id}"
        )
    return f"found {len(results)} memories:\n\n" + "\n\n".join(output)


@mcp.tool
async def relate(
    from_entity: str,
    to_entity: str,
    relation_type: str = "related_to",
    tenant: str | None = None,
) -> str:
    """Create a directed relation between two entities in the knowledge graph.

    Args:
        from_entity: source entity name
        to_entity: target entity name
        relation_type: type of relation (related_to, depends_on, produces, consumes, uses)
        tenant: project/tenant scope
    """
    pg = _get_pg()
    provider = _get_provider()
    t = tenant or _config.default_tenant

    await create_entity(pg, from_entity, "concept", t, provider=provider)
    await create_entity(pg, to_entity, "concept", t, provider=provider)

    rid = await create_relation_by_name(pg, from_entity, to_entity, relation_type, t)
    if rid:
        return f"created relation: {from_entity} --[{relation_type}]--> {to_entity}"
    return f"failed to create relation (entities may not exist in tenant '{t}')"


@mcp.tool
async def forget(memory_id: str) -> str:
    """Soft-delete a memory by ID.

    Args:
        memory_id: UUID of the memory to delete
    """
    pg = _get_pg()
    repo = MemoryRepository(pg)
    rows = await repo.soft_delete(memory_id)
    if rows:
        cache = _get_cache()
        await cache.invalidate_memory(UUID(memory_id))
        return f"memory {memory_id} soft-deleted"
    return f"memory {memory_id} not found or already deleted"


@mcp.tool
async def graph_query(
    entity: str,
    hops: int = 2,
    relation_types: str | None = None,
    tenant: str | None = None,
) -> str:
    """Traverse the knowledge graph starting from an entity.

    Args:
        entity: starting entity name
        hops: max traversal depth (default 2)
        relation_types: comma-separated relation types to filter (e.g. "depends_on,produces")
        tenant: project/tenant scope
    """
    pg = _get_pg()
    t = tenant or _config.default_tenant

    rtypes = relation_types.split(",") if relation_types else None
    nodes = await traverse(pg, entity, t, max_hops=hops, relation_types=rtypes)

    if not nodes:
        return f"no graph data found for entity '{entity}' in tenant '{t}'"

    output = []
    for n in nodes:
        rel = f" via [{n.relation_type}]" if n.relation_type else ""
        path_str = " → ".join(n.path)
        output.append(f"  depth={n.depth} ({n.entity_type}) {n.entity_name}{rel}  path: {path_str}")

    return f"graph traversal from '{entity}' ({len(nodes)} nodes):\n" + "\n".join(output)


@mcp.tool
async def list_entities_tool(
    tenant: str | None = None,
    entity_type: str | None = None,
    limit: int = 50,
) -> str:
    """List known entities in the knowledge graph.

    Args:
        tenant: project/tenant scope
        entity_type: filter by type (service, concept, pattern, etc.)
        limit: max entities to return
    """
    pg = _get_pg()
    t = tenant or _config.default_tenant
    repo = EntityRepository(pg)

    entities = await repo.list(t, entity_type=entity_type, limit=limit)

    if not entities:
        return f"no entities found in tenant '{t}'"

    lines = [f"  ({e['entity_type']}) {e['name']}" for e in entities]
    return f"{len(entities)} entities in '{t}':\n" + "\n".join(lines)


@mcp.tool
async def memory_stats(tenant: str | None = None) -> str:
    """Show memory statistics — counts by type, depth layer, and decay distribution.

    Args:
        tenant: project/tenant scope (None = all tenants)
    """
    pg = _get_pg()
    mem_repo = MemoryRepository(pg)
    ent_repo = EntityRepository(pg)
    rel_repo = RelationRepository(pg)

    by_type = await mem_repo.count_by_type(tenant)
    by_depth = await mem_repo.count_by_depth(tenant)
    by_tenant = await mem_repo.count_by_tenant(tenant)
    entity_count = await ent_repo.count(tenant)
    relation_count = await rel_repo.count()

    lines = ["=== synapto memory stats ==="]
    total = sum(r["cnt"] for r in by_type)
    lines.append(f"total memories: {total}")
    lines.append(f"total entities: {entity_count}")
    lines.append(f"total relations: {relation_count}")
    lines.append("\nby type:")
    for r in by_type:
        lines.append(f"  {r['type']}: {r['cnt']}")
    lines.append("\nby depth layer:")
    for r in by_depth:
        lines.append(f"  {r['depth_layer']}: {r['cnt']}")
    if not tenant:
        lines.append("\nby tenant:")
        for r in by_tenant:
            lines.append(f"  {r['tenant']}: {r['cnt']}")

    return "\n".join(lines)


@mcp.tool
async def maintain() -> str:
    """Run maintenance tasks — update decay scores, cleanup ephemeral memories."""
    pg = _get_pg()
    updated = await update_decay_scores(pg)
    cleaned = await cleanup_ephemeral(pg, _config.decay_ephemeral_max_age_hours)
    return f"maintenance complete: {updated} decay scores updated, {cleaned} ephemeral memories cleaned"


@mcp.tool
async def trust_feedback(memory_id: str, helpful: bool) -> str:
    """Adjust a memory's trust score based on feedback.

    Trust is adjusted asymmetrically: helpful +0.05, unhelpful -0.10.
    This ensures bad memories are demoted faster than good ones are promoted.

    Args:
        memory_id: UUID of the memory
        helpful: whether the memory was helpful
    """
    pg = _get_pg()
    repo = MemoryRepository(pg)
    delta = 0.05 if helpful else -0.10

    rows = await repo.update_trust(memory_id, delta)
    if rows:
        new_score = rows[0]["trust_score"]
        direction = "boosted" if helpful else "penalized"
        return f"memory {memory_id} {direction} — trust_score now {new_score:.2f}"
    return f"memory {memory_id} not found or already deleted"


@mcp.tool
async def find_contradictions(tenant: str | None = None, threshold: float = 0.3) -> str:
    """Detect potentially contradictory memories.

    Finds memory pairs that share entities (same subject) but have divergent
    content (different claims). Useful for memory hygiene.

    Args:
        tenant: project/tenant scope
        threshold: minimum contradiction score to report (0.0-1.0)
    """
    pg = _get_pg()
    t = tenant or _config.default_tenant

    results = await hrr_contradict(pg, tenant=t, threshold=threshold)
    if not results:
        return "no contradictions found"

    output = [f"found {len(results)} potential contradiction(s):\n"]
    for c in results:
        output.append(
            f"score={c.contradiction_score:.3f} "
            f"(entity_overlap={c.entity_overlap:.2f}, content_sim={c.content_similarity:.2f})\n"
            f"  A: [{c.memory_a.depth_layer}] {c.memory_a.content[:120]}...\n"
            f"  B: [{c.memory_b.depth_layer}] {c.memory_b.content[:120]}...\n"
            f"  shared: {', '.join(c.shared_entities)}"
        )
    return "\n\n".join(output)
