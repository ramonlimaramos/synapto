"""Repository for memory scope membership.

Design pattern: Repository — isolates all memory_scopes SQL behind a
domain-oriented API built on :class:`~synapto.scopes.ScopeSet`.

Two properties this module has to guarantee, neither of which the schema can:

**Set semantics under concurrency.** Membership is set-valued — "these are the
scopes of this memory" — and the aggregate rules (``global`` does not combine,
at most 20 scopes) span rows, so no row-local ``CHECK`` can enforce them. Two
concurrent replacements on the same memory could otherwise both delete an empty
set and commit different rows, leaving their union rather than one replacement.
Every mutation therefore takes a ``FOR UPDATE`` lock on the parent memory row
first, which serializes writers per memory.

**A transaction boundary a caller can own.** The ``*_on`` primitives take a
caller-supplied connection, so a memory write and its scope write can commit or
roll back together. The convenience wrappers just open a transaction and
delegate — they are not a second implementation.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from synapto.db.postgres import PostgresClient
from synapto.scopes import ScopeRef, ScopeSet
from synapto.sql import scopes as sql

DEFAULT_SOURCE = "explicit"


class UnknownMemoryError(LookupError):
    """Raised when a scope mutation targets a memory that does not exist."""


def _as_uuid(value: UUID | str) -> UUID:
    """Coerce an id to :class:`UUID`.

    The batch query binds a ``uuid[]``, and psycopg refuses a heterogeneous
    list, so a mix of ``UUID`` and ``str`` — which callers naturally produce
    when ids come from both the database and JSON — must be normalized first.
    """
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    raise TypeError(f"memory id must be a UUID or str, got {type(value).__name__}")


class ScopeRepository:
    """Data access for memory scope membership."""

    def __init__(self, client: PostgresClient) -> None:
        self._db = client

    # -- connection-scoped primitives ---------------------------------------
    #
    # Take a caller-owned connection so the parent memory write and the scope
    # write share one transaction.

    async def replace_on(self, conn, memory_id: UUID | str, scopes: ScopeSet, *, source: str = DEFAULT_SOURCE) -> int:
        """Replace a memory's scopes, on a caller-supplied connection.

        Locks the parent memory row first, so concurrent replacements on the
        same memory serialize instead of interleaving into their union.

        Raises:
            UnknownMemoryError: the memory does not exist.
        """
        memory_uuid = _as_uuid(memory_id)
        await self._lock_memory(conn, memory_uuid)

        await conn.execute(sql.DELETE_ALL, {"memory_id": memory_uuid})
        for ref in scopes:
            await conn.execute(sql.INSERT, self._insert_params(memory_uuid, ref, source))
        return len(scopes)

    async def clear_on(self, conn, memory_id: UUID | str) -> None:
        """Remove every scope from a memory, on a caller-supplied connection.

        Raises:
            UnknownMemoryError: the memory does not exist.
        """
        memory_uuid = _as_uuid(memory_id)
        await self._lock_memory(conn, memory_uuid)
        await conn.execute(sql.DELETE_ALL, {"memory_id": memory_uuid})

    # -- self-managed wrappers ----------------------------------------------

    async def replace(self, memory_id: UUID | str, scopes: ScopeSet, *, source: str = DEFAULT_SOURCE) -> int:
        """Replace a memory's scopes in its own transaction.

        Passing an empty set clears the membership.
        """
        async with self._db.acquire() as conn:
            return await self.replace_on(conn, memory_id, scopes, source=source)

    async def clear(self, memory_id: UUID | str) -> None:
        """Remove every scope from a memory, in its own transaction."""
        async with self._db.acquire() as conn:
            await self.clear_on(conn, memory_id)

    # -- reads ---------------------------------------------------------------

    async def get_for_memory(self, memory_id: UUID | str) -> ScopeSet:
        """Return one memory's scopes, ordered by ``(scope_type, scope_key)``."""
        rows = await self._db.execute(sql.SELECT_FOR_MEMORY, {"memory_id": _as_uuid(memory_id)})
        return ScopeSet(scopes=tuple(ScopeRef(row["scope_type"], row["scope_key"]) for row in rows))

    async def get_for_memories(self, memory_ids: Sequence[UUID | str]) -> dict[UUID, ScopeSet]:
        """Return scopes for many memories in a single query.

        Batched because the read path renders scopes for every recall hit, and
        one query per hit would put an N+1 on the hot path. An empty request
        issues no query at all.

        Memories with no scopes are absent from the result rather than mapped to
        an empty set, so callers can distinguish "has no scopes" from "was not
        requested".

        Rehydration goes through the validating :class:`ScopeRef` constructor,
        so a row that somehow violates the contract fails closed here rather
        than propagating a corrupt scope to a caller.
        """
        if not memory_ids:
            return {}

        unique_ids = list(dict.fromkeys(_as_uuid(memory_id) for memory_id in memory_ids))
        rows = await self._db.execute(sql.SELECT_FOR_MEMORIES, {"memory_ids": unique_ids})

        grouped: dict[UUID, list[ScopeRef]] = {}
        for row in rows:
            grouped.setdefault(row["memory_id"], []).append(ScopeRef(row["scope_type"], row["scope_key"]))
        # SQL already orders by (memory_id, scope_type, scope_key), so per-memory
        # ordering is preserved by insertion order without re-sorting
        return {memory_id: ScopeSet(scopes=tuple(refs)) for memory_id, refs in grouped.items()}

    # -- internals -----------------------------------------------------------

    @staticmethod
    async def _lock_memory(conn, memory_id: UUID) -> None:
        cursor = await conn.execute(sql.LOCK_MEMORY, {"memory_id": memory_id})
        if await cursor.fetchone() is None:
            raise UnknownMemoryError(f"memory {memory_id} does not exist")

    @staticmethod
    def _insert_params(memory_id: UUID, ref: ScopeRef, source: str) -> dict[str, object]:
        return {
            "memory_id": memory_id,
            "scope_type": ref.scope_type,
            "scope_key": ref.scope_key,
            "source": source,
        }
