"""Statements for ``tenant_aliases`` and the memory move a merge performs.

``LOCK_TABLE`` takes ``SHARE ROW EXCLUSIVE`` so the chain check and the insert
that follows it cannot interleave with another registration; the one-hop
invariant those two statements protect is explained in
:mod:`synapto.repositories.tenants`. ``MOVE_MEMORIES`` binds by name, the rest
positionally. No format slots.
"""

RESOLVE = """
    SELECT canonical FROM tenant_aliases WHERE alias = %s;
"""

LOCK_TABLE = """
    LOCK TABLE tenant_aliases IN SHARE ROW EXCLUSIVE MODE;
"""

IS_ALIAS = """
    SELECT canonical FROM tenant_aliases WHERE alias = %s FOR UPDATE;
"""

HAS_ALIASES = """
    SELECT 1 FROM tenant_aliases WHERE canonical = %s LIMIT 1;
"""

INSERT = """
    INSERT INTO tenant_aliases (alias, canonical) VALUES (%s, %s)
    ON CONFLICT (alias) DO UPDATE SET canonical = EXCLUDED.canonical
    RETURNING alias, canonical;
"""

LIST = """
    SELECT alias, canonical FROM tenant_aliases ORDER BY canonical, alias;
"""

MOVE_MEMORIES = """
    UPDATE memories SET tenant = %(canonical)s WHERE tenant = %(alias)s;
"""
