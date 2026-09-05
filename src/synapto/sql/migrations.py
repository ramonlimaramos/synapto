"""Statements for the ``synapto_migrations`` ledger and the HNSW indexes.

Format slots:

* ``HNSW_INDEX_TEMPLATE`` — ``{table}``, one of the fixed pair
  ``("memories", "entities")``, and ``{dim}``, the integer embedding dimension.
  The index is dimension-dependent, which is why it is created here rather than
  shipped as a migration file.

``RECORD_APPLIED`` is ``ON CONFLICT DO NOTHING`` so re-recording a filename —
the legacy-schema bridge does this for ``001_initial.sql`` — is idempotent.
This module must stay importable without third-party dependencies; it holds
strings only.
"""

TRACKING_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS synapto_migrations (
    id SERIAL PRIMARY KEY,
    filename VARCHAR(255) NOT NULL UNIQUE,
    checksum VARCHAR(64) NOT NULL,
    applied_at TIMESTAMPTZ DEFAULT now()
);
"""

SELECT_APPLIED = "SELECT filename, checksum FROM synapto_migrations ORDER BY filename;"

RECORD_APPLIED = (
    "INSERT INTO synapto_migrations (filename, checksum) VALUES (%s, %s) ON CONFLICT (filename) DO NOTHING;"
)

FORGET_APPLIED = "DELETE FROM synapto_migrations WHERE filename = %s;"

LEGACY_SCHEMA_EXISTS = "SELECT 1 FROM information_schema.tables WHERE table_name = 'synapto_schema';"

HNSW_INDEX_TEMPLATE = """
    CREATE INDEX IF NOT EXISTS idx_{table}_embedding_{dim}
    ON {table} USING hnsw ((embedding::vector({dim})) vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
"""
