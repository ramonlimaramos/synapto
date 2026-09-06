"""Fixtures shared by the ablation and the layer-spread sweep."""

from __future__ import annotations

import pytest

from synapto.db.migrations import run_migrations
from synapto.hrr.core import DEFAULT_DIM, encode_fact, phases_to_bytes

TWINS_TENANT = "acme/ablation"
TWINS_CONTENT = "the deploy pipeline requires a signed tag before it publishes"
TWINS_QUERY = "signed tag before publish"


@pytest.fixture
async def twins(pg, provider):
    """Two identical memories, ``core`` then ``ephemeral``, so relevance is constant and only weight varies.

    Both carry an HRR vector, as a memory stored through ``remember`` would,
    so ``full`` really does add an HRR leg that ``no-hrr`` must remove.

    Migrations run first because ``tests/eval`` sorts before ``tests/unit``
    and may be the first thing to touch a fresh database.
    """
    await run_migrations(pg)
    await pg.execute("DELETE FROM memories WHERE tenant = %s;", (TWINS_TENANT,))
    embedding = await provider.embed_one(TWINS_CONTENT)
    hrr_vector = phases_to_bytes(encode_fact(TWINS_CONTENT, []))
    ids = []
    for layer in ("core", "ephemeral"):
        row = await pg.execute_one(
            """
            INSERT INTO memories (content, embedding, embedding_dim, type, tenant, depth_layer, hrr_vector, hrr_dim)
            VALUES (%s, %s, %s, 'general', %s, %s, %s, %s) RETURNING id;
            """,
            (TWINS_CONTENT, embedding, provider.dimension, TWINS_TENANT, layer, hrr_vector, DEFAULT_DIM),
        )
        ids.append(row["id"])
    yield tuple(ids)
    await pg.execute("DELETE FROM memories WHERE tenant = %s;", (TWINS_TENANT,))
