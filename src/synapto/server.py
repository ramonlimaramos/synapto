"""Synapto MCP server — exposes memory graph tools via the Model Context Protocol."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
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
from synapto.prompts import load_prompt
from synapto.repositories.entities import EntityRepository
from synapto.repositories.memories import MemoryRepository
from synapto.repositories.relations import RelationRepository
from synapto.search.graph import traverse
from synapto.search.hybrid import hybrid_search
from synapto.telemetry import instrumented_tool

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


SERVER_INSTRUCTIONS = load_prompt("server_instructions")

# Marks a tool so Claude Code (and any MCP client that honors `_meta.alwaysLoad`)
# skips the deferred-tool `ToolSearch` handshake and loads the schema eagerly.
# Reserved for the two tools the LLM is expected to call in most conversations.
ALWAYS_LOAD_META = {"alwaysLoad": True}
MAX_RECALL_PREVIEW_CHARS = 1000
DEFAULT_RECALL_PREVIEW_CHARS = 200
MAX_BULK_MEMORY_IDS = 20


def _wrap_system_reminder(body: str) -> str:
    """Wrap recall output so Claude Code treats it as injected context, not a user message.

    Claude Code recognizes `<system-reminder>...</system-reminder>` blocks and folds them
    into the conversation as contextual hints (see `src/utils/messages.ts`). Other MCP
    clients will render the tags verbatim — harmless but not as seamless.
    """
    return f"<system-reminder>\n{body.strip()}\n</system-reminder>"


def _format_timestamp(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat()


def _format_json(value: dict[str, Any] | None) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)


def _format_memory(
    row: dict[str, Any],
    *,
    include_entities: bool = False,
    entities: list[dict[str, Any]] | None = None,
    include_relations: bool = False,
    relations: list[dict[str, Any]] | None = None,
) -> str:
    lines = [
        f"id: {row['id']}",
        f"tenant: {row['tenant']}",
        f"type: {row['type']}",
        f"depth_layer: {row['depth_layer']}",
        f"trust_score: {float(row.get('trust_score', 0.5)):.2f}",
        f"decay_score: {float(row.get('decay_score', 1.0)):.2f}",
        f"access_count: {row.get('access_count', 0)}",
        f"created_at: {_format_timestamp(row.get('created_at'))}",
        f"accessed_at: {_format_timestamp(row.get('accessed_at'))}",
    ]
    if row.get("summary"):
        lines.append(f"summary: {row['summary']}")
    lines.append(f"metadata: {_format_json(row.get('metadata'))}")

    if include_entities:
        entity_names = [e["name"] for e in entities or []]
        lines.append(f"entities: {', '.join(entity_names) if entity_names else '(none)'}")

    if include_relations:
        rel_lines = [f"{r['from_entity']} --[{r['relation_type']}]--> {r['to_entity']}" for r in relations or []]
        lines.append("relations:")
        if rel_lines:
            lines.extend(f"  {line}" for line in rel_lines)
        else:
            lines.append("  (none)")

    lines.append("content:")
    lines.append(row["content"])
    return "\n".join(lines)


def _parse_memory_ids(memory_ids: list[str]) -> tuple[list[UUID], list[str]]:
    valid_ids: list[UUID] = []
    invalid_ids: list[str] = []
    for memory_id in memory_ids:
        try:
            valid_ids.append(UUID(memory_id))
        except ValueError:
            invalid_ids.append(memory_id)
    return valid_ids, invalid_ids


mcp = FastMCP("synapto", instructions=SERVER_INSTRUCTIONS, lifespan=_lifespan)


@mcp.tool(meta=ALWAYS_LOAD_META)
@instrumented_tool
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


@mcp.tool(meta=ALWAYS_LOAD_META)
@instrumented_tool
async def recall(
    query: str,
    tenant: str | None = None,
    depth_layer: str | None = None,
    limit: int = 10,
    preview_chars: int = DEFAULT_RECALL_PREVIEW_CHARS,
) -> str:
    """Search memories using hybrid semantic + keyword search with RRF ranking.

    Args:
        query: natural language search query
        tenant: filter to a specific project/tenant
        depth_layer: filter to a specific depth layer (core, stable, working, ephemeral)
        limit: max results to return
        preview_chars: max content characters per result (0-1000)
    """
    pg = _get_pg()
    provider = _get_provider()
    t = tenant or _config.default_tenant
    preview_chars = max(0, min(preview_chars, MAX_RECALL_PREVIEW_CHARS))

    results = await hybrid_search(pg, provider, query, tenant=t, depth_layer=depth_layer, limit=limit)

    if not results:
        return _wrap_system_reminder(load_prompt("recall_empty"))

    memories = []
    for r in results:
        preview = r.content[:preview_chars]
        if preview_chars and len(r.content) > preview_chars:
            preview += "..."
        memories.append(
            f"[{r.depth_layer}] ({r.type}) score={r.rrf_score:.4f} "
            f"decay={r.decay_score:.2f} trust={r.trust_score:.2f} "
            f"tenant={r.tenant} created_at={_format_timestamp(r.created_at)}\n"
            f"  {preview}\n"
            f"  id={r.id}"
        )
    body = f"{load_prompt('recall_preamble')}\nRecalled {len(results)} memories:\n\n" + "\n\n".join(memories)
    return _wrap_system_reminder(body)


@mcp.tool
@instrumented_tool
async def get_memory(
    memory_id: str,
    include_entities: bool = True,
    include_relations: bool = False,
) -> str:
    """Fetch a complete memory by ID after recall identifies a relevant hit.

    Args:
        memory_id: UUID of the memory to fetch
        include_entities: include entities linked to the memory
        include_relations: include relations for linked entities
    """
    pg = _get_pg()
    try:
        parsed_id = UUID(memory_id)
    except ValueError:
        return f"invalid memory id: {memory_id}"

    mem_repo = MemoryRepository(pg)
    row = await mem_repo.get_by_id(parsed_id)
    if not row:
        return f"memory {memory_id} not found or deleted"

    entities = []
    relations = []
    if include_entities or include_relations:
        ent_repo = EntityRepository(pg)
        entities = await ent_repo.get_memory_entities(parsed_id)

    if include_relations and entities:
        rel_repo = RelationRepository(pg)
        seen_relation_ids: set[UUID] = set()
        for entity in entities:
            for relation in await rel_repo.get_relations(entity["name"], row["tenant"]):
                if relation["id"] in seen_relation_ids:
                    continue
                seen_relation_ids.add(relation["id"])
                relations.append(relation)

    return _format_memory(
        row,
        include_entities=include_entities,
        entities=entities,
        include_relations=include_relations,
        relations=relations,
    )


@mcp.tool
@instrumented_tool
async def get_memories(
    memory_ids: list[str],
    include_entities: bool = False,
) -> str:
    """Fetch multiple complete memories by ID, preserving the requested order.

    Args:
        memory_ids: UUIDs of memories to fetch (max 20)
        include_entities: include entities linked to each memory
    """
    if len(memory_ids) > MAX_BULK_MEMORY_IDS:
        return f"too many memory ids: max {MAX_BULK_MEMORY_IDS}, got {len(memory_ids)}"

    valid_ids, invalid_ids = _parse_memory_ids(memory_ids)
    rows = []
    if valid_ids:
        pg = _get_pg()
        mem_repo = MemoryRepository(pg)
        rows = await mem_repo.get_by_ids(valid_ids)
    by_id = {str(row["id"]): row for row in rows}

    entities_by_id: dict[str, list[dict[str, Any]]] = {}
    if include_entities and rows:
        pg = _get_pg()
        ent_repo = EntityRepository(pg)
        for row in rows:
            entities_by_id[str(row["id"])] = await ent_repo.get_memory_entities(row["id"])

    output = []
    invalid = set(invalid_ids)
    for memory_id in memory_ids:
        if memory_id in invalid:
            output.append(f"memory {memory_id}: invalid id")
            continue

        row = by_id.get(memory_id)
        if not row:
            output.append(f"memory {memory_id}: not found or deleted")
            continue

        output.append(
            _format_memory(
                row,
                include_entities=include_entities,
                entities=entities_by_id.get(memory_id),
            )
        )

    return "\n\n---\n\n".join(output) if output else "no memory ids provided"


@mcp.tool
@instrumented_tool
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
@instrumented_tool
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
@instrumented_tool
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
@instrumented_tool
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
@instrumented_tool
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
@instrumented_tool
async def maintain() -> str:
    """Run maintenance tasks — update decay scores, cleanup ephemeral memories."""
    pg = _get_pg()
    updated = await update_decay_scores(pg)
    cleaned = await cleanup_ephemeral(pg, _config.decay_ephemeral_max_age_hours)
    return f"maintenance complete: {updated} decay scores updated, {cleaned} ephemeral memories cleaned"


@mcp.tool
@instrumented_tool
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
@instrumented_tool
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
