"""Seeding and querying the golden corpus, shared by the gate and the ablation.

The corpus goes in through ``server.remember`` — the same path a client takes,
so entity extraction, HRR encoding and scope storage are exercised rather than
bypassed with a raw insert. Trust and decay overrides are applied afterwards
with plain updates because no tool sets them directly.

Insertion order is the caller's to decide: the gate seeds in file order, the
ablation reseeds in shuffled orders to measure how much of a metric is owed
to ``created_at`` tie-breaking alone.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from types import SimpleNamespace
from uuid import UUID

from synapto import server
from synapto.scopes import ScopeSet
from synapto.search.hybrid import hybrid_search
from tests.eval.harness import MRR_CUTOFF, TENANT, Case, Memory, Outcome

STORED_ID = re.compile(r"stored memory ([0-9a-f-]{36})")
SET_TRUST = "UPDATE memories SET trust_score = %s WHERE id = %s;"
SET_DECAY = "UPDATE memories SET decay_score = %s WHERE id = %s;"
DELETE_MEMORIES = "DELETE FROM memories WHERE tenant = %s;"
DELETE_ENTITIES = "DELETE FROM entities WHERE tenant = %s;"
DELETE_BANKS = "DELETE FROM memory_banks WHERE bank_name LIKE %s;"


def wire_server(monkeypatch, pg, provider, cache) -> None:
    """Point the MCP tools at the test database, provider and cache."""
    monkeypatch.setattr(server, "_pg", pg)
    monkeypatch.setattr(server, "_provider", provider)
    monkeypatch.setattr(server, "_cache", cache)
    monkeypatch.setattr(server, "_config", SimpleNamespace(default_tenant=TENANT))


async def seed_corpus(pg, memories: Iterable[Memory]) -> dict[UUID, str]:
    """Store every memory in the given order and map each stored id back to its key."""
    keys_by_id: dict[UUID, str] = {}
    for memory in memories:
        keys_by_id[await seed(pg, memory)] = memory.key
    return keys_by_id


async def seed(pg, memory: Memory) -> UUID:
    reply = await server.remember(
        content=memory.content,
        memory_type=memory.memory_type,
        tenant=TENANT,
        depth_layer=memory.depth_layer,
        metadata=memory.metadata or None,
        scopes=list(memory.scopes) or None,
    )
    memory_id = UUID(STORED_ID.search(reply).group(1))
    if memory.trust is not None:
        await pg.execute(SET_TRUST, (memory.trust, memory_id))
    if memory.decay is not None:
        await pg.execute(SET_DECAY, (memory.decay, memory_id))
    return memory_id


async def clean(pg) -> None:
    await pg.execute(DELETE_MEMORIES, (TENANT,))
    await pg.execute(DELETE_ENTITIES, (TENANT,))
    await pg.execute(DELETE_BANKS, (f"{TENANT}:%",))


async def run_case(pg, provider, case: Case, keys_by_id: dict[UUID, str]) -> Outcome:
    results = await hybrid_search(
        pg,
        provider,
        case.query,
        tenant=TENANT,
        depth_layer=case.depth_layer,
        limit=MRR_CUTOFF,
        scopes=ScopeSet.parse(list(case.scopes)) if case.scopes else None,
        metadata_filter=case.metadata_filter,
    )
    return Outcome(case=case, ranked=tuple(keys_by_id[result.id] for result in results))


async def run_cases(pg, provider, cases: Iterable[Case], keys_by_id: dict[UUID, str]) -> list[Outcome]:
    return [await run_case(pg, provider, case, keys_by_id) for case in cases]
