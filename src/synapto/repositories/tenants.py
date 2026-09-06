"""Tenant alias storage — one hop, never a chain.

``tenant_aliases`` maps a superseded tenant spelling to the one that survived a
merge, so reads of a folded tenant keep finding what was written under it.

The invariant the table cannot express row-locally is that a canonical must
never itself be an alias. Enforcing it here, rather than resolving transitively
at read time, is a deliberate trade: transitive resolution would make a read's
cost depend on chain depth and would loop on a cycle, while refusing the second
hop at write time keeps every read exactly one lookup and makes the cycle
impossible to create. The cost is that re-pointing a merged tenant is an
explicit two-step operation rather than an accidental one.
"""

from __future__ import annotations

from collections.abc import Sequence

from synapto.db.postgres import PostgresClient
from synapto.sql import tenants as sql
from synapto.tenants import InvalidTenantError, is_canonical_tenant, validate_tenant


class TenantAliasError(RuntimeError):
    """A tenant alias would break the one-hop invariant."""


class TenantAliasRepository:
    """Reads and writes the superseded-tenant map."""

    def __init__(self, client: PostgresClient) -> None:
        self._db = client

    async def resolve(self, tenant: str) -> str:
        """Return the canonical tenant for ``tenant``, or ``tenant`` unchanged.

        One lookup, one hop. An unknown tenant is not an error: the overwhelming
        majority of reads name a tenant that was never merged, and treating that
        as a miss worth reporting would make the common path the noisy one.
        """
        row = await self._db.execute_one(sql.RESOLVE, (tenant,))
        return row["canonical"] if row else tenant

    async def register(self, alias: str, canonical: str) -> None:
        """Record that ``alias`` was folded into ``canonical``.

        Both must be canonical tenants, and they must differ — the table checks
        that too, but failing here names the argument rather than the
        constraint.

        Raises:
            InvalidTenantError: either value is not a canonical tenant.
            TenantAliasError: the mapping would create a chain, in either
                direction — ``canonical`` is itself an alias, or ``alias`` is
                already the survivor of some other merge.
        """
        validate_tenant(alias, source="alias")
        validate_tenant(canonical, source="canonical")
        if alias == canonical:
            raise TenantAliasError(f"tenant {alias!r} cannot be an alias of itself")

        async with self._db.acquire() as conn:
            await conn.execute(sql.LOCK_TABLE)
            await self._reject_chain(conn, alias, canonical)
            await conn.execute(sql.INSERT, (alias, canonical))

    @staticmethod
    async def _reject_chain(conn, alias: str, canonical: str) -> None:
        """Refuse both directions of a two-hop mapping.

        The lock is taken by the caller before either check, so a concurrent
        registration cannot slip between the read and the insert and build the
        chain this refuses.
        """
        cursor = await conn.execute(sql.IS_ALIAS, (canonical,))
        existing = await cursor.fetchone()
        if existing:
            raise TenantAliasError(
                f"{canonical!r} is itself an alias of {existing['canonical']!r}; "
                f"point {alias!r} at {existing['canonical']!r} instead of creating a chain"
            )

        cursor = await conn.execute(sql.HAS_ALIASES, (alias,))
        if await cursor.fetchone():
            raise TenantAliasError(
                f"{alias!r} is already the canonical tenant of other aliases; "
                "repoint those first, or they would become a chain"
            )

    async def list_aliases(self) -> list[dict]:
        """Every recorded mapping, ordered by canonical then alias."""
        return await self._db.execute(sql.LIST)

    async def merge(self, spelling: str, canonical: str) -> int:
        """Fold one stored tenant spelling into ``canonical``; see :meth:`merge_all`."""
        return await self.merge_all([(spelling, canonical)])

    async def merge_all(self, plan: Sequence[tuple[str, str]]) -> int:
        """Apply every ``(spelling, canonical)`` move in ``plan`` as one transaction.

        ``canonical`` must be canonical. ``spelling`` is whatever the store
        holds, canonical or not: a legacy tenant written before the grammar
        existed (``Acme/API``) is exactly what a merge is for, and refusing to
        fold it would leave it unreachable forever, since every tool rejects
        that spelling at the boundary. The alias row is recorded only when the
        spelling is itself canonical. A non-canonical spelling can never be
        looked up — the boundary rejects it and names the lowercase form before
        any alias is consulted — so a row for it would be dead, and the table's
        grammar check refuses it anyway.

        One transaction for the whole plan, not one per move: a plan is what a
        human approved as a unit, and a refusal on the third group after the
        first two moved would leave the store in a state nobody approved. Every
        argument is validated before the transaction opens, so a malformed
        plan fails without touching the database.

        Returns:
            The number of memories moved.

        Raises:
            InvalidTenantError: a canonical is not canonical, or a spelling is
                not a non-empty string.
            TenantAliasError: a move would create a chain or fold a tenant
                into itself.
        """
        for spelling, canonical in plan:
            _validate_move(spelling, canonical)

        moved = 0
        async with self._db.acquire() as conn:
            await conn.execute(sql.LOCK_TABLE)
            for spelling, canonical in plan:
                moved += await self._move(conn, spelling, canonical)
        return moved

    async def _move(self, conn, spelling: str, canonical: str) -> int:
        await self._reject_chain(conn, spelling, canonical)
        cursor = await conn.execute(sql.MOVE_MEMORIES, {"alias": spelling, "canonical": canonical})
        if is_canonical_tenant(spelling):
            await conn.execute(sql.INSERT, (spelling, canonical))
        return cursor.rowcount


def _validate_move(spelling: object, canonical: str) -> None:
    if not isinstance(spelling, str) or not spelling:
        raise InvalidTenantError(f"spelling must be a non-empty string, got {spelling!r}")
    validate_tenant(canonical, source="canonical")
    if spelling == canonical:
        raise TenantAliasError(f"tenant {spelling!r} cannot be an alias of itself")
