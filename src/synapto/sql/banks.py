"""Statements for ``memory_banks``, plus the type listing that feeds bank rebuilds.

All parameters are positional ``%s``. No format slots.
"""

UPSERT = """
    INSERT INTO memory_banks (bank_name, vector, dim, fact_count, updated_at)
    VALUES (%s, %s, %s, %s, now())
    ON CONFLICT (bank_name) DO UPDATE SET
        vector = EXCLUDED.vector,
        dim = EXCLUDED.dim,
        fact_count = EXCLUDED.fact_count,
        updated_at = now();
"""

DELETE = "DELETE FROM memory_banks WHERE bank_name = %s;"

GET_VECTOR = "SELECT vector FROM memory_banks WHERE bank_name = %s;"

LIST_TYPES = "SELECT DISTINCT type FROM memories WHERE tenant = %s AND deleted_at IS NULL;"
