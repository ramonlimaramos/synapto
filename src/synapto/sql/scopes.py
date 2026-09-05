"""Statements for ``memory_scopes``.

All parameters bind by name. ``LOCK_MEMORY`` is ``FOR UPDATE`` on the parent
row: set-valued membership rules span rows, so no row-local ``CHECK`` can hold
them, and serializing writers per memory is what keeps two concurrent
replacements from committing their union (see
:mod:`synapto.repositories.scopes`). No format slots.
"""

LOCK_MEMORY = """
    SELECT id FROM memories WHERE id = %(memory_id)s FOR UPDATE;
"""

INSERT = """
    INSERT INTO memory_scopes (memory_id, scope_type, scope_key, source)
    VALUES (%(memory_id)s, %(scope_type)s, %(scope_key)s, %(source)s)
    ON CONFLICT (memory_id, scope_type, scope_key) DO NOTHING;
"""

DELETE_ALL = "DELETE FROM memory_scopes WHERE memory_id = %(memory_id)s;"

SELECT_FOR_MEMORY = """
    SELECT scope_type, scope_key
    FROM memory_scopes
    WHERE memory_id = %(memory_id)s
    ORDER BY scope_type, scope_key;
"""

SELECT_FOR_MEMORIES = """
    SELECT memory_id, scope_type, scope_key
    FROM memory_scopes
    WHERE memory_id = ANY(%(memory_ids)s::uuid[])
    ORDER BY memory_id, scope_type, scope_key;
"""
