"""Entity extraction, creation, and management for Synapto's knowledge graph."""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from synapto.db.postgres import PostgresClient
from synapto.embeddings.base import EmbeddingProvider

logger = logging.getLogger("synapto.graph.entities")


async def create_entity(
    client: PostgresClient,
    name: str,
    entity_type: str = "concept",
    tenant: str = "default",
    metadata: dict[str, Any] | None = None,
    provider: EmbeddingProvider | None = None,
) -> UUID:
    """Create or update an entity, returning its ID."""
    embedding = None
    dim = None
    if provider:
        embedding = await provider.embed_one(name)
        dim = provider.dimension

    row = await client.execute_one(
        """
        INSERT INTO entities (name, entity_type, tenant, metadata, embedding, embedding_dim)
        VALUES (%(name)s, %(type)s, %(tenant)s, %(meta)s, %(emb)s, %(dim)s)
        ON CONFLICT (name, tenant) DO UPDATE SET
            entity_type = EXCLUDED.entity_type,
            metadata = entities.metadata || EXCLUDED.metadata,
            embedding = COALESCE(EXCLUDED.embedding, entities.embedding),
            embedding_dim = COALESCE(EXCLUDED.embedding_dim, entities.embedding_dim)
        RETURNING id;
        """,
        {
            "name": name,
            "type": entity_type,
            "tenant": tenant,
            "meta": Jsonb(metadata or {}),
            "emb": embedding,
            "dim": dim,
        },
    )
    return row["id"]


async def get_entity(
    client: PostgresClient, name: str, tenant: str = "default"
) -> dict[str, Any] | None:
    return await client.execute_one(
        "SELECT * FROM entities WHERE name = %s AND tenant = %s;",
        (name, tenant),
    )


async def list_entities(
    client: PostgresClient,
    tenant: str = "default",
    entity_type: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if entity_type:
        return await client.execute(
            "SELECT id, name, entity_type, tenant, metadata, created_at "
            "FROM entities WHERE tenant = %s AND entity_type = %s "
            "ORDER BY name LIMIT %s;",
            (tenant, entity_type, limit),
        )
    return await client.execute(
        "SELECT id, name, entity_type, tenant, metadata, created_at "
        "FROM entities WHERE tenant = %s ORDER BY name LIMIT %s;",
        (tenant, limit),
    )


async def delete_entity(client: PostgresClient, name: str, tenant: str = "default") -> bool:
    rows = await client.execute(
        "DELETE FROM entities WHERE name = %s AND tenant = %s RETURNING id;",
        (name, tenant),
    )
    return len(rows) > 0


async def link_memory_to_entity(
    client: PostgresClient, memory_id: UUID, entity_id: UUID
) -> None:
    await client.execute(
        """
        INSERT INTO memory_entities (memory_id, entity_id)
        VALUES (%s, %s) ON CONFLICT DO NOTHING;
        """,
        (memory_id, entity_id),
    )


async def get_memory_entities(
    client: PostgresClient, memory_id: UUID
) -> list[dict[str, Any]]:
    return await client.execute(
        """
        SELECT e.id, e.name, e.entity_type
        FROM entities e
        JOIN memory_entities me ON me.entity_id = e.id
        WHERE me.memory_id = %s;
        """,
        (memory_id,),
    )


def extract_entities_from_text(text: str) -> list[str]:
    """Simple entity extraction — finds capitalized phrases and quoted terms.

    This is a lightweight heuristic. For production use, consider spaCy NER.
    """
    entities: set[str] = set()

    # capitalized multi-word phrases (e.g., "Machine Learning", "Redis Cache")
    for match in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", text):
        entities.add(match.group(1))

    # single capitalized words that aren't sentence starters (heuristic: not after ". ")
    for match in re.finditer(r"(?<!\.\s)(?<!\A)\b([A-Z][a-z]{2,})\b", text):
        entities.add(match.group(1))

    # backtick-quoted terms (common in technical text)
    for match in re.finditer(r"`([^`]+)`", text):
        entities.add(match.group(1))

    # remove common false positives
    stop_words = {"The", "This", "That", "These", "Those", "When", "Where", "Which", "What", "How"}
    entities -= stop_words

    return sorted(entities)
