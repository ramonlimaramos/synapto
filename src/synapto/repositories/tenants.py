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

from synapto.db.postgres import PostgresClient
from synapto.sql import tenants as sql
from synapto.tenants import validate_tenant


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

    async def merge(self, alias: str, canonical: str) -> int:
        """Move every memory from ``alias`` to ``canonical`` and record the alias.

        The move and the alias registration share one transaction: a partial
        apply would leave memories under a tenant with no alias pointing away
        from it, which is exactly the silent unreachability this whole change
        exists to remove.

        Returns:
            The number of memories moved.
        """
        validate_tenant(alias, source="alias")
        validate_tenant(canonical, source="canonical")
        if alias == canonical:
            raise TenantAliasError(f"tenant {alias!r} cannot be an alias of itself")

        async with self._db.acquire() as conn:
            await conn.execute(sql.LOCK_TABLE)
            await self._reject_chain(conn, alias, canonical)
            cursor = await conn.execute(sql.MOVE_MEMORIES, {"alias": alias, "canonical": canonical})
            moved = cursor.rowcount
            await conn.execute(sql.INSERT, (alias, canonical))
        return moved
