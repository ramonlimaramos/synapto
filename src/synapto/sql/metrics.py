"""Statements for ``metrics_events``.

``INSERT`` binds by name; the reads and the purge bind positionally. The two
``LIST_BY_NAME_*`` variants exist instead of one statement with an optional
predicate: the caller picks one, it does not assemble one. No format slots.
"""

INSERT = """
    INSERT INTO metrics_events (name, type, value, tags)
    VALUES (%(name)s, %(type)s, %(value)s, %(tags)s);
"""

LIST_BY_NAME_NO_SINCE = """
    SELECT id, name, type, value, tags, created_at
    FROM metrics_events
    WHERE name = %s
    ORDER BY created_at DESC, id DESC
    LIMIT %s;
"""

LIST_BY_NAME_SINCE = """
    SELECT id, name, type, value, tags, created_at
    FROM metrics_events
    WHERE name = %s AND created_at >= %s
    ORDER BY created_at DESC, id DESC
    LIMIT %s;
"""

PURGE_OLDER = """
    DELETE FROM metrics_events
    WHERE created_at < now() - make_interval(days => %s);
"""
