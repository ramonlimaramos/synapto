"""Repository for memory CRUD, search, decay, and trust operations.

Design pattern: Repository — isolates all memory-table SQL behind a domain-oriented API.
Consumers never see raw SQL; they call methods like create(), soft_delete(), update_trust().
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from synapto.db.postgres import PostgresClient
from synapto.repositories.scopes import ScopeRepository, UnknownMemoryError, _as_uuid
from synapto.scopes import ScopeSet, reject_conflicting_scope_arguments

# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------

_INSERT = """
    INSERT INTO memories
        (content, summary, embedding, embedding_dim, type, subtype, domain, tenant, depth_layer, metadata)
    VALUES (
        %(content)s, %(summary)s, %(emb)s, %(dim)s, %(type)s, %(subtype)s,
        %(domain)s, %(tenant)s, %(depth)s, %(meta)s
    )
    RETURNING id;
"""

_GET_BY_ID = """
    SELECT
        id,
        content,
        summary,
        type,
        subtype,
        domain,
        tenant,
        depth_layer,
        metadata,
        decay_score,
        trust_score,
        access_count,
        created_at,
        accessed_at
    FROM memories
    WHERE id = %s AND deleted_at IS NULL;
"""

_GET_BY_IDS = """
    SELECT
        id,
        content,
        summary,
        type,
        subtype,
        domain,
        tenant,
        depth_layer,
        metadata,
        decay_score,
        trust_score,
        access_count,
        created_at,
        accessed_at
    FROM memories
    WHERE id = ANY(%s::uuid[]) AND deleted_at IS NULL;
"""

_UPDATE_HRR = "UPDATE memories SET hrr_vector = %s, hrr_dim = %s WHERE id = %s;"

_UPDATE_MEMORY = """
    UPDATE memories
    SET
        content = CASE WHEN %(content_provided)s THEN %(content)s ELSE content END,
        summary = CASE WHEN %(summary_provided)s THEN %(summary)s ELSE summary END,
        embedding = CASE WHEN %(embedding_provided)s THEN %(emb)s::vector ELSE embedding END,
        embedding_dim = CASE WHEN %(dim_provided)s THEN %(dim)s ELSE embedding_dim END,
        metadata = CASE
            WHEN %(meta_provided)s THEN COALESCE(metadata, '{}'::jsonb) || %(meta)s::jsonb
            ELSE metadata
        END,
        accessed_at = now()
    WHERE id = %(id)s AND deleted_at IS NULL
    RETURNING
        id,
        content,
        summary,
        type,
        subtype,
        domain,
        tenant,
        depth_layer,
        metadata,
        decay_score,
        trust_score,
        access_count,
        created_at,
        accessed_at;
"""

# FOR UPDATE, not a plain SELECT: a check that only reads leaves a window in
# which another transaction can soft-delete the memory or move it to a different
# tenant before the scope write lands. Locking here makes the qualification and
# the mutation one atomic step — under READ COMMITTED the row is re-checked
# after the lock is granted, so a concurrent delete or ownership change causes
# this query to return nothing rather than authorizing a stale view.
_AUTHORIZE_MEMORY = """
    SELECT id FROM memories
    WHERE id = %(memory_id)s AND tenant = %(tenant)s AND deleted_at IS NULL
    FOR UPDATE;
"""

_SOFT_DELETE = """
    UPDATE memories SET deleted_at = now()
    WHERE id = %s AND deleted_at IS NULL
    RETURNING id;
"""

_UPDATE_TRUST = """
    UPDATE memories
    SET trust_score = GREATEST(0.0, LEAST(1.0, trust_score + %s))
    WHERE id = %s AND deleted_at IS NULL
    RETURNING id, trust_score;
"""

_TOUCH_ACCESSED = """
    UPDATE memories SET accessed_at = now(), access_count = access_count + 1
    WHERE id = ANY(%s);
"""

_SELECT_FOR_DECAY = """
    SELECT id, depth_layer, created_at, accessed_at, access_count
    FROM memories
    WHERE deleted_at IS NULL
    ORDER BY accessed_at ASC
    LIMIT %s;
"""

_UPDATE_DECAY_SCORE = "UPDATE memories SET decay_score = %s WHERE id = %s;"

_CLEANUP_EPHEMERAL = """
    UPDATE memories SET deleted_at = now()
    WHERE depth_layer = 'ephemeral'
      AND deleted_at IS NULL
      AND accessed_at < now() - make_interval(hours => %s)
    RETURNING id;
"""

_PURGE_DELETED = """
    DELETE FROM memories
    WHERE deleted_at IS NOT NULL
      AND deleted_at < now() - make_interval(days => %s)
    RETURNING id;
"""

_SELECT_HRR_VECTORS = """
    SELECT hrr_vector FROM memories
    WHERE {where_clause};
"""

_SELECT_WITH_HRR = """
    SELECT id, content, type, subtype, tenant, depth_layer, trust_score, hrr_vector
    FROM memories
    WHERE {where_clause}
    LIMIT %s;
"""

_COUNT_BY_TYPE = """
    SELECT type, count(*) as cnt FROM memories
    {where_clause} GROUP BY type ORDER BY cnt DESC;
"""

_COUNT_BY_DEPTH = """
    SELECT depth_layer, count(*) as cnt FROM memories
    {where_clause} GROUP BY depth_layer ORDER BY cnt DESC;
"""

_COUNT_BY_TENANT = """
    SELECT tenant, count(*) as cnt FROM memories
    {where_clause} GROUP BY tenant ORDER BY cnt DESC;
"""

_SELECT_ORIGINAL_FILES = """
    SELECT metadata->>'original_file' AS original_file
    FROM memories
    WHERE tenant = %s
      AND deleted_at IS NULL
      AND metadata ? 'original_file';
"""


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


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
        }

        if not scopes:
            row = await self._db.execute_one(_INSERT, params)
            return row["id"]

        async with self._db.acquire() as conn:
            cursor = await conn.execute(_INSERT, params)
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
        cursor = await conn.execute(_AUTHORIZE_MEMORY, {"memory_id": memory_id, "tenant": tenant})
        if await cursor.fetchone() is None:
            # deliberately indistinguishable from "does not exist": telling a
            # caller that someone else's memory id is real leaks tenancy
            raise UnknownMemoryError(f"memory {memory_id} does not exist for tenant {tenant!r} or has been deleted")

    async def update_hrr(self, memory_id: UUID, hrr_vector: bytes, hrr_dim: int) -> None:
        await self._db.execute(_UPDATE_HRR, (hrr_vector, hrr_dim, memory_id))

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
                _UPDATE_MEMORY,
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
            _UPDATE_MEMORY,
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
        row = await self._db.execute_one(_GET_BY_ID, (memory_id,))
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

        rows = await self._db.execute(_GET_BY_IDS, (requested,))
        if include_scopes and rows:
            by_memory = await ScopeRepository(self._db).get_for_memories([row["id"] for row in rows])
            for row in rows:
                row["scopes"] = by_memory.get(row["id"], ScopeSet())

        by_id = {row["id"]: row for row in rows}
        return [by_id[memory_id] for memory_id in requested if memory_id in by_id]

    async def soft_delete(self, memory_id: str) -> list[dict]:
        return await self._db.execute(_SOFT_DELETE, (memory_id,))

    async def update_trust(self, memory_id: str, delta: float) -> list[dict]:
        return await self._db.execute(_UPDATE_TRUST, (delta, memory_id))

    async def touch_accessed(self, ids: list[UUID]) -> None:
        await self._db.execute(_TOUCH_ACCESSED, (ids,))

    # -- decay & maintenance --

    async def select_for_decay(self, batch_size: int = 500) -> list[dict]:
        return await self._db.execute(_SELECT_FOR_DECAY, (batch_size,))

    async def update_decay_scores(self, updates: list[tuple[float, UUID]]) -> None:
        await self._db.execute_many(_UPDATE_DECAY_SCORE, updates)

    async def cleanup_ephemeral(self, max_age_hours: int) -> list[dict]:
        return await self._db.execute(_CLEANUP_EPHEMERAL, (max_age_hours,))

    async def purge_deleted(self, older_than_days: int) -> list[dict]:
        return await self._db.execute(_PURGE_DELETED, (older_than_days,))

    # -- hrr vectors --

    async def select_hrr_vectors(
        self, tenant: str, type_filter: str | None = None, depth_filter: str | None = None
    ) -> list[dict]:
        where = ["deleted_at IS NULL", "tenant = %s", "hrr_vector IS NOT NULL"]
        params: list = [tenant]
        if type_filter:
            where.append("type = %s")
            params.append(type_filter)
        if depth_filter:
            where.append("depth_layer = %s")
            params.append(depth_filter)
        sql = _SELECT_HRR_VECTORS.format(where_clause=" AND ".join(where))
        return await self._db.execute(sql, tuple(params))

    # -- hrr retrieval --

    async def select_with_hrr(self, tenant: str, depth_layer: str | None = None, limit: int = 100) -> list[dict]:
        where = ["deleted_at IS NULL", "tenant = %s", "hrr_vector IS NOT NULL"]
        params: list = [tenant]
        if depth_layer:
            where.append("depth_layer = %s")
            params.append(depth_layer)
        sql = _SELECT_WITH_HRR.format(where_clause=" AND ".join(where))
        return await self._db.execute(sql, (*params, limit))

    # -- stats --

    async def count_by_type(self, tenant: str | None = None) -> list[dict]:
        where, params = self._tenant_filter(tenant)
        return await self._db.execute(_COUNT_BY_TYPE.format(where_clause=where), params)

    async def count_by_depth(self, tenant: str | None = None) -> list[dict]:
        where, params = self._tenant_filter(tenant)
        return await self._db.execute(_COUNT_BY_DEPTH.format(where_clause=where), params)

    async def count_by_tenant(self, tenant: str | None = None) -> list[dict]:
        where, params = self._tenant_filter(tenant)
        return await self._db.execute(_COUNT_BY_TENANT.format(where_clause=where), params)

    async def find_existing_original_files(self, tenant: str) -> set[str]:
        rows = await self._db.execute(_SELECT_ORIGINAL_FILES, (tenant,))
        return {r["original_file"] for r in rows if r.get("original_file")}

    @staticmethod
    def _tenant_filter(tenant: str | None) -> tuple[str, tuple]:
        if tenant:
            return "WHERE deleted_at IS NULL AND tenant = %s", (tenant,)
        return "WHERE deleted_at IS NULL", ()
