"""Statements for ``memories``: CRUD, trust, decay, maintenance, and HRR reads.

Format slots, each filled from a constant in this module or left empty:

* ``SELECT_HRR_VECTORS`` — ``{type_filter}`` (``HRR_TYPE_FILTER``) and
  ``{depth_filter}`` (``HRR_DEPTH_FILTER``). Positional parameters follow the
  text: ``tenant``, then ``type`` if filtered, then ``depth_layer`` if filtered.
* ``SELECT_WITH_HRR`` — ``{depth_filter}`` (``HRR_DEPTH_FILTER``); parameters
  are ``tenant``, ``depth_layer`` if filtered, then ``limit``.
* ``COUNT_BY_TYPE`` / ``COUNT_BY_DEPTH`` / ``COUNT_BY_TENANT`` —
  ``{where_clause}``: ``WHERE_LIVE`` (no parameters) or ``WHERE_LIVE_IN_TENANT``
  (``tenant``).

``AUTHORIZE_MEMORY`` is ``FOR UPDATE``, not a plain ``SELECT``: a check that only
reads leaves a window in which another transaction can soft-delete the memory or
move it to a different tenant before a scope write lands. Locking makes the
qualification and the mutation one atomic step — under ``READ COMMITTED`` the
row is re-checked after the lock is granted, so a concurrent delete or ownership
change makes this return nothing rather than authorize a stale view.

``UPDATE_MEMORY`` takes a ``*_provided`` flag per column so one statement serves
every partial update; the flags are booleans the repository derives from which
arguments were passed, never text.
"""

INSERT = """
    INSERT INTO memories
        (content, summary, embedding, embedding_dim, type, subtype, domain, tenant, depth_layer, metadata,
         origin)
    VALUES (
        %(content)s, %(summary)s, %(emb)s, %(dim)s, %(type)s, %(subtype)s,
        %(domain)s, %(tenant)s, %(depth)s, %(meta)s, %(origin)s
    )
    RETURNING id;
"""

GET_BY_ID = """
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
        origin,
        decay_score,
        trust_score,
        access_count,
        created_at,
        accessed_at
    FROM memories
    WHERE id = %s AND deleted_at IS NULL;
"""

GET_BY_IDS = """
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
        origin,
        decay_score,
        trust_score,
        access_count,
        created_at,
        accessed_at
    FROM memories
    WHERE id = ANY(%s::uuid[]) AND deleted_at IS NULL;
"""

UPDATE_HRR = "UPDATE memories SET hrr_vector = %s, hrr_dim = %s WHERE id = %s;"

UPDATE_MEMORY = """
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
        origin,
        decay_score,
        trust_score,
        access_count,
        created_at,
        accessed_at;
"""

AUTHORIZE_MEMORY = """
    SELECT id FROM memories
    WHERE id = %(memory_id)s AND tenant = %(tenant)s AND deleted_at IS NULL
    FOR UPDATE;
"""

SOFT_DELETE = """
    UPDATE memories SET deleted_at = now()
    WHERE id = %(memory_id)s AND deleted_at IS NULL
    RETURNING id;
"""

SELECT_ORIGIN = """
    SELECT origin FROM memories WHERE id = %s AND deleted_at IS NULL;
"""

UPDATE_TRUST = """
    UPDATE memories
    SET trust_score = GREATEST(0.0, LEAST(1.0, trust_score + %s))
    WHERE id = %s AND deleted_at IS NULL
    RETURNING id, trust_score;
"""

TOUCH_ACCESSED = """
    UPDATE memories SET accessed_at = now(), access_count = access_count + 1
    WHERE id = ANY(%s);
"""

SELECT_FOR_DECAY = """
    SELECT id, depth_layer, created_at, accessed_at, access_count
    FROM memories
    WHERE deleted_at IS NULL
    ORDER BY accessed_at ASC
    LIMIT %s;
"""

UPDATE_DECAY_SCORE = "UPDATE memories SET decay_score = %s WHERE id = %s;"

CLEANUP_EPHEMERAL = """
    UPDATE memories SET deleted_at = now()
    WHERE depth_layer = 'ephemeral'
      AND deleted_at IS NULL
      AND accessed_at < now() - make_interval(hours => %s)
    RETURNING id;
"""

PURGE_DELETED = """
    DELETE FROM memories
    WHERE deleted_at IS NOT NULL
      AND deleted_at < now() - make_interval(days => %s)
    RETURNING id;
"""

SELECT_HRR_VECTORS = """
    SELECT hrr_vector FROM memories
    WHERE deleted_at IS NULL AND tenant = %s AND hrr_vector IS NOT NULL {type_filter} {depth_filter};
"""

SELECT_WITH_HRR = """
    SELECT id, content, type, subtype, tenant, depth_layer, trust_score, hrr_vector
    FROM memories
    WHERE deleted_at IS NULL AND tenant = %s AND hrr_vector IS NOT NULL {depth_filter}
    LIMIT %s;
"""

HRR_TYPE_FILTER = "AND type = %s"

HRR_DEPTH_FILTER = "AND depth_layer = %s"

COUNT_BY_TYPE = """
    SELECT type, count(*) as cnt FROM memories
    {where_clause} GROUP BY type ORDER BY cnt DESC;
"""

COUNT_BY_DEPTH = """
    SELECT depth_layer, count(*) as cnt FROM memories
    {where_clause} GROUP BY depth_layer ORDER BY cnt DESC;
"""

COUNT_BY_TENANT = """
    SELECT tenant, count(*) as cnt FROM memories
    {where_clause} GROUP BY tenant ORDER BY cnt DESC;
"""

WHERE_LIVE = "WHERE deleted_at IS NULL"

WHERE_LIVE_IN_TENANT = "WHERE deleted_at IS NULL AND tenant = %s"

SELECT_ORIGINAL_FILES = """
    SELECT metadata->>'original_file' AS original_file
    FROM memories
    WHERE tenant = %s
      AND deleted_at IS NULL
      AND metadata ? 'original_file';
"""
