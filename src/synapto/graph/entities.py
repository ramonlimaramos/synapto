"""Entity extraction, creation, and management for Synapto's knowledge graph."""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

from synapto.db.postgres import PostgresClient
from synapto.embeddings.base import EmbeddingProvider
from synapto.repositories.entities import EntityRepository

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

    repo = EntityRepository(client)
    return await repo.upsert(
        name=name,
        entity_type=entity_type,
        tenant=tenant,
        metadata=metadata,
        embedding=embedding,
        embedding_dim=dim,
    )


async def get_entity(
    client: PostgresClient, name: str, tenant: str = "default"
) -> dict[str, Any] | None:
    return await EntityRepository(client).get_by_name(name, tenant)


async def list_entities(
    client: PostgresClient,
    tenant: str = "default",
    entity_type: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    return await EntityRepository(client).list(tenant, entity_type=entity_type, limit=limit)


async def delete_entity(client: PostgresClient, name: str, tenant: str = "default") -> bool:
    return await EntityRepository(client).delete(name, tenant)


async def link_memory_to_entity(
    client: PostgresClient, memory_id: UUID, entity_id: UUID
) -> None:
    await EntityRepository(client).link_memory(memory_id, entity_id)


async def get_memory_entities(
    client: PostgresClient, memory_id: UUID
) -> list[dict[str, Any]]:
    return await EntityRepository(client).get_memory_entities(memory_id)


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
