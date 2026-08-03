"""Repository for memory scope membership.

Design pattern: Repository — isolates all memory_scopes SQL behind a
domain-oriented API built on :class:`~synapto.scopes.ScopeSet`.

Membership changes are set-valued: "these are the scopes of this memory" rather
than "add one row". Every mutation therefore runs its statements on a single
pooled connection inside one transaction, so a memory is never observable with
half of a replacement applied.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from synapto.db.postgres import PostgresClient
from synapto.scopes import ScopeRef, ScopeSet

# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------

_INSERT = """
    INSERT INTO memory_scopes (memory_id, scope_type, scope_key, source)
    VALUES (%(memory_id)s, %(scope_type)s, %(scope_key)s, %(source)s)
    ON CONFLICT (memory_id, scope_type, scope_key) DO NOTHING;
"""

_DELETE_ALL = "DELETE FROM memory_scopes WHERE memory_id = %(memory_id)s;"

_SELECT_FOR_MEMORY = """
    SELECT scope_type, scope_key
    FROM memory_scopes
    WHERE memory_id = %(memory_id)s
    ORDER BY scope_type, scope_key;
"""

_SELECT_FOR_MEMORIES = """
    SELECT memory_id, scope_type, scope_key
    FROM memory_scopes
    WHERE memory_id = ANY(%(memory_ids)s)
    ORDER BY memory_id, scope_type, scope_key;
"""

DEFAULT_SOURCE = "explicit"


class ScopeRepository:
    """Data access for memory scope membership."""

    def __init__(self, client: PostgresClient) -> None:
        self._db = client

    async def add(self, memory_id: UUID | str, scopes: ScopeSet, *, source: str = DEFAULT_SOURCE) -> int:
        """Add scopes to a memory, leaving existing memberships in place.

        Idempotent: re-adding an existing scope is a no-op rather than an error.
        Returns the number of scopes submitted.
        """
        if not scopes:
            return 0

        async with self._db.acquire() as conn:
            for ref in scopes:
                await conn.execute(_INSERT, self._insert_params(memory_id, ref, source))
        return len(scopes)

    async def replace(self, memory_id: UUID | str, scopes: ScopeSet, *, source: str = DEFAULT_SOURCE) -> int:
        """Replace a memory's scopes with ``scopes``, atomically.

        The delete and the inserts share one connection and one transaction, so
        a failure part-way leaves the previous membership intact rather than a
        partially applied set. Passing an empty set clears the membership.
        """
        async with self._db.acquire() as conn:
            await conn.execute(_DELETE_ALL, {"memory_id": memory_id})
            for ref in scopes:
                await conn.execute(_INSERT, self._insert_params(memory_id, ref, source))
        return len(scopes)

    async def clear(self, memory_id: UUID | str) -> None:
        """Remove every scope from a memory."""
        async with self._db.acquire() as conn:
            await conn.execute(_DELETE_ALL, {"memory_id": memory_id})

    async def get_for_memory(self, memory_id: UUID | str) -> ScopeSet:
        """Return one memory's scopes, ordered by ``(scope_type, scope_key)``."""
        rows = await self._db.execute(_SELECT_FOR_MEMORY, {"memory_id": memory_id})
        return ScopeSet(scopes=tuple(ScopeRef(row["scope_type"], row["scope_key"]) for row in rows))

    async def get_for_memories(self, memory_ids: Sequence[UUID | str]) -> dict[UUID, ScopeSet]:
        """Return scopes for many memories in one query.

        Batched on purpose: the read path renders scopes for every recall hit,
        and one query per hit would put an N+1 on the hot path. Memories with no
        scopes are absent from the result rather than mapped to an empty set —
        callers distinguish "no scopes" from "not requested" by the key.
        """
        if not memory_ids:
            return {}

        rows = await self._db.execute(_SELECT_FOR_MEMORIES, {"memory_ids": list(memory_ids)})

        grouped: dict[UUID, list[ScopeRef]] = {}
        for row in rows:
            grouped.setdefault(row["memory_id"], []).append(ScopeRef(row["scope_type"], row["scope_key"]))
        # SQL already orders by (memory_id, scope_type, scope_key), so per-memory
        # ordering is preserved by insertion without re-sorting
        return {memory_id: ScopeSet(scopes=tuple(refs)) for memory_id, refs in grouped.items()}

    @staticmethod
    def _insert_params(memory_id: UUID | str, ref: ScopeRef, source: str) -> dict[str, object]:
        return {
            "memory_id": memory_id,
            "scope_type": ref.scope_type,
            "scope_key": ref.scope_key,
            "source": source,
        }
