"""Repository for memory CRUD, search, decay, and trust operations.

Design pattern: Repository — isolates all memory-table SQL behind a domain-oriented API.
Consumers never see raw SQL; they call methods like create(), soft_delete(), update_trust().
The statements themselves live in :mod:`synapto.sql.memories`; this module
binds parameters and chooses between the filter fragments that module names.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from synapto.db.postgres import PostgresClient
from synapto.provenance import DEFAULT_ORIGIN, validate_origin
from synapto.repositories.scopes import ScopeRepository, UnknownMemoryError, _as_uuid
from synapto.scopes import ScopeSet, reject_conflicting_scope_arguments
from synapto.sql import memories as sql


class MemoryRepository:
    """Encapsulates all memory-table SQL operations."""

    def __init__(self, client: PostgresClient) -> None:
        self._db = client

    async def create(
        self,
        content: str,
        embedding: list[float],
        embedding_dim: int,
        memory_type: str,
        tenant: str,
        depth_layer: str,
        subtype: str | None = None,
        summary: str | None = None,
        metadata: dict[str, Any] | None = None,
        *,
        domain: str | None = None,
        scopes: ScopeSet | None = None,
        origin: str = DEFAULT_ORIGIN,
    ) -> UUID:
        """Create a memory, optionally with its initial scopes.

        ``domain`` and ``scopes`` are keyword-only. ``domain`` had been inserted
        between ``subtype`` and ``summary``, silently rebinding the summary of
        any positional caller; moving both after the established positional
        parameters restores that contract.

        When ``scopes`` is given, the memory row and its memberships commit or
        roll back together — a memory must never become visible without the
        scopes that say where it applies. Omitting it, or passing an empty set,
        creates an unscoped memory.
        """
        reject_conflicting_scope_arguments(domain, scopes)

        params = {
            "content": content,
            "summary": summary,
            "emb": embedding,
            "dim": embedding_dim,
            "type": memory_type,
            "subtype": subtype,
            "domain": domain,
            "tenant": tenant,
            "depth": depth_layer,
            "meta": Jsonb(metadata or {}),
            "origin": validate_origin(origin),
        }

        if not scopes:
            row = await self._db.execute_one(sql.INSERT, params)
            return row["id"]

        async with self._db.acquire() as conn:
            cursor = await conn.execute(sql.INSERT, params)
            memory_id = (await cursor.fetchone())["id"]
            await ScopeRepository(self._db).replace_on(conn, memory_id, scopes)
        return memory_id

    # -- scope membership ----------------------------------------------------

    async def replace_scopes(self, memory_id: str | UUID, scopes: ScopeSet | None, *, tenant: str) -> None:
        """Set a memory's scopes, authorizing the parent first.

        ``ScopeRepository`` is an ID-only primitive with no notion of tenancy,
        so reaching it through a raw memory id would let one tenant rescope
        another's memory. Authorization and the mutation share one transaction,
        so the parent cannot be soft-deleted or moved between the check and the
        write.

        ``None`` preserves the existing scopes and does nothing; an empty set
        clears them; a non-empty set replaces them.
        """
        if scopes is None:
            return

        async with self._db.acquire() as conn:
            await self._authorize(conn, memory_id, tenant)
            await ScopeRepository(self._db).replace_on(conn, memory_id, scopes)

    async def clear_scopes(self, memory_id: str | UUID, *, tenant: str) -> None:
        """Remove every scope from a memory the tenant owns."""
        async with self._db.acquire() as conn:
            await self._authorize(conn, memory_id, tenant)
            await ScopeRepository(self._db).clear_on(conn, memory_id)

    @staticmethod
    async def _authorize(conn, memory_id: str | UUID, tenant: str) -> None:
        cursor = await conn.execute(sql.AUTHORIZE_MEMORY, {"memory_id": memory_id, "tenant": tenant})
        if await cursor.fetchone() is None:
            # deliberately indistinguishable from "does not exist": telling a
            # caller that someone else's memory id is real leaks tenancy
            raise UnknownMemoryError(f"memory {memory_id} does not exist for tenant {tenant!r} or has been deleted")

    async def update_hrr(self, memory_id: UUID, hrr_vector: bytes, hrr_dim: int) -> None:
        await self._db.execute(sql.UPDATE_HRR, (hrr_vector, hrr_dim, memory_id))

    async def update_with_scopes(
        self,
        memory_id: str | UUID,
        *,
        tenant: str,
        content: str | None = None,
        embedding: list[float] | None = None,
        embedding_dim: int | None = None,
        summary: str | None = None,
        metadata_patch: dict[str, Any] | None = None,
        scopes: ScopeSet | None = None,
    ) -> dict[str, Any] | None:
        """Update a memory's fields and its scopes in one transaction.

        Without this, a caller updating both would commit the field change
        through one connection and the scope change through another: a scope
        failure would leave the content updated and the memberships stale, which
        is exactly the partial commit the mutation contract forbids.

        The parent is locked and authorized once, and every write rides that
        same connection. Scope semantics match :meth:`replace_scopes` — ``None``
        preserves, an empty set clears, a non-empty set replaces.

        Work is O(s) for s scopes, capped at 20, in one transaction.
        """
        async with self._db.acquire() as conn:
            await self._authorize(conn, memory_id, tenant)

            cursor = await conn.execute(
                sql.UPDATE_MEMORY,
                self._update_params(
                    memory_id,
                    content=content,
                    embedding=embedding,
                    embedding_dim=embedding_dim,
                    summary=summary,
                    metadata_patch=metadata_patch,
                ),
            )
            row = await cursor.fetchone()

            if scopes is not None:
                await ScopeRepository(self._db).replace_on(conn, memory_id, scopes)
                if row is not None:
                    row["scopes"] = scopes

        return row

    @staticmethod
    def _update_params(
        memory_id: str | UUID,
        *,
        content: str | None,
        embedding: list[float] | None,
        embedding_dim: int | None,
        summary: str | None,
        metadata_patch: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "id": memory_id,
            "content_provided": content is not None,
            "content": content,
            "summary_provided": summary is not None,
            "summary": summary,
            "embedding_provided": embedding is not None,
            "emb": embedding,
            "dim_provided": embedding_dim is not None,
            "dim": embedding_dim,
            "meta_provided": metadata_patch is not None,
            "meta": Jsonb(metadata_patch or {}),
        }

    async def update(
        self,
        memory_id: str | UUID,
        *,
        content: str | None = None,
        embedding: list[float] | None = None,
        embedding_dim: int | None = None,
        summary: str | None = None,
        metadata_patch: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        return await self._db.execute_one(
            sql.UPDATE_MEMORY,
            self._update_params(
                memory_id,
                content=content,
                embedding=embedding,
                embedding_dim=embedding_dim,
                summary=summary,
                metadata_patch=metadata_patch,
            ),
        )

    async def get_by_id(self, memory_id: str | UUID, *, include_scopes: bool = True) -> dict[str, Any] | None:
        """Fetch one active memory, carrying its ordered scopes by default."""
        row = await self._db.execute_one(sql.GET_BY_ID, (memory_id,))
        if row is None:
            return None
        if include_scopes:
            row["scopes"] = await ScopeRepository(self._db).get_for_memory(row["id"])
        return row

    async def get_by_ids(self, memory_ids: list[str | UUID], *, include_scopes: bool = True) -> list[dict[str, Any]]:
        """Fetch active memories in the order requested, carrying ordered scopes.

        Ids are normalized to :class:`UUID` first: the query binds a ``uuid[]``
        and psycopg refuses a list mixing ``UUID`` and ``str``, which callers
        produce naturally when ids arrive from both the database and JSON.

        The result is the **ordered subsequence** of the unique active requested
        ids: requested order is preserved, a duplicated id yields its row once
        at the first requested position, and an id with no active row — missing
        or soft-deleted — is absent rather than a placeholder.

        Because entries can be missing, the result must never be zipped against
        the requested list: ``[missing, existing]`` returns ``[existing]``, and
        zipping would silently attribute that row to the missing id. Callers
        needing correlation must map by ``row["id"]``.

        Scopes come from one batched query rather than one per row: this feeds
        the recall render path, where a per-memory lookup would be an N+1 on
        every result page.
        """
        if not memory_ids:
            return []

        requested = list(dict.fromkeys(_as_uuid(memory_id) for memory_id in memory_ids))

        rows = await self._db.execute(sql.GET_BY_IDS, (requested,))
        if include_scopes and rows:
            by_memory = await ScopeRepository(self._db).get_for_memories([row["id"] for row in rows])
            for row in rows:
                row["scopes"] = by_memory.get(row["id"], ScopeSet())

        by_id = {row["id"]: row for row in rows}
        return [by_id[memory_id] for memory_id in requested if memory_id in by_id]

    async def soft_delete(self, memory_id: str) -> list[dict]:
        return await self._db.execute(sql.SOFT_DELETE, {"memory_id": memory_id})

    async def get_origin(self, memory_id: str | UUID) -> str | None:
        """Return a live memory's recorded origin, or None if it is gone.

        Read rather than inferred, every time. The value is whatever the writer
        declared; nothing here reconstructs it from the row's shape or age.
        """
        row = await self._db.execute_one(sql.SELECT_ORIGIN, (memory_id,))
        return row["origin"] if row else None

    async def update_trust(self, memory_id: str, delta: float) -> list[dict]:
        return await self._db.execute(sql.UPDATE_TRUST, (delta, memory_id))

    async def touch_accessed(self, ids: list[UUID]) -> None:
        await self._db.execute(sql.TOUCH_ACCESSED, (ids,))

    # -- decay & maintenance --

    async def select_for_decay(self, batch_size: int = 500) -> list[dict]:
        return await self._db.execute(sql.SELECT_FOR_DECAY, (batch_size,))

    async def update_decay_scores(self, updates: list[tuple[float, UUID]]) -> None:
        await self._db.execute_many(sql.UPDATE_DECAY_SCORE, updates)

    async def cleanup_ephemeral(self, max_age_hours: int) -> list[dict]:
        return await self._db.execute(sql.CLEANUP_EPHEMERAL, (max_age_hours,))

    async def purge_deleted(self, older_than_days: int) -> list[dict]:
        return await self._db.execute(sql.PURGE_DELETED, (older_than_days,))

    # -- hrr vectors --

    async def select_hrr_vectors(
        self, tenant: str, type_filter: str | None = None, depth_filter: str | None = None
    ) -> list[dict]:
        params: list = [tenant]
        if type_filter:
            params.append(type_filter)
        if depth_filter:
            params.append(depth_filter)
        statement = sql.SELECT_HRR_VECTORS.format(
            type_filter=sql.HRR_TYPE_FILTER if type_filter else "",
            depth_filter=sql.HRR_DEPTH_FILTER if depth_filter else "",
        )
        return await self._db.execute(statement, tuple(params))

    # -- hrr retrieval --

    async def select_with_hrr(self, tenant: str, depth_layer: str | None = None, limit: int = 100) -> list[dict]:
        params: list = [tenant]
        if depth_layer:
            params.append(depth_layer)
        statement = sql.SELECT_WITH_HRR.format(depth_filter=sql.HRR_DEPTH_FILTER if depth_layer else "")
        return await self._db.execute(statement, (*params, limit))

    # -- stats --

    async def count_by_type(self, tenant: str | None = None) -> list[dict]:
        where, params = self._tenant_filter(tenant)
        return await self._db.execute(sql.COUNT_BY_TYPE.format(where_clause=where), params)

    async def count_by_depth(self, tenant: str | None = None) -> list[dict]:
        where, params = self._tenant_filter(tenant)
        return await self._db.execute(sql.COUNT_BY_DEPTH.format(where_clause=where), params)

    async def count_by_tenant(self, tenant: str | None = None) -> list[dict]:
        where, params = self._tenant_filter(tenant)
        return await self._db.execute(sql.COUNT_BY_TENANT.format(where_clause=where), params)

    async def find_existing_original_files(self, tenant: str) -> set[str]:
        rows = await self._db.execute(sql.SELECT_ORIGINAL_FILES, (tenant,))
        return {r["original_file"] for r in rows if r.get("original_file")}

    @staticmethod
    def _tenant_filter(tenant: str | None) -> tuple[str, tuple]:
        if tenant:
            return sql.WHERE_LIVE_IN_TENANT, (tenant,)
        return sql.WHERE_LIVE, ()
