"""Statements behind ``hybrid_search``, ``vector_search`` and ``count_memories``.

Format slots:

* ``RRF_QUERY_TEMPLATE`` and ``VECTOR_ONLY_TEMPLATE`` — ``{dim}``, always the
  integer ``provider.dimension``, and ``{{filters}}`` (doubled because ``dim``
  is formatted first). ``COUNT`` — ``{filters}`` only.
* ``{filters}`` is a newline-joined selection of the ``FILTER_*`` constants and
  ``SCOPE_FILTER`` below, or the empty string. The values they compare against
  travel as named parameters; the filter text is chosen, never written.

**RRF pre-selection.** Each leg ranks its own top 20, the outer query sums the
reciprocal ranks and multiplies by ``decay × trust × layer_weight`` to pick the
candidates. HRR scoring happens in Python because it reads ``bytea`` vectors
PostgreSQL cannot rank, so the outer ``LIMIT`` is the caller's ``2 × limit`` and
Python applies the same weight after adding the HRR boost. The layer weights
are spelled out twice — once to select ``quality_weight``, once to order — and
``synapto.search.hybrid.DEPTH_BOOST`` mirrors them; a test asserts the two
agree, so nothing generates the ``CASE`` arms.

**Applicability (``SCOPE_FILTER``).** One correlated predicate so no join fans
out and RRF ranking stays intact. Reading it inside-out: the memory must have at
least one scope, which excludes unscoped legacy rows whenever a filter is
requested; ``global:all`` short-circuits to a match; otherwise every scope type
the memory carries must be satisfied by at least one requested value of that
type. Grouping by ``scope_type`` gives OR within a type for free, and
``NOT EXISTS`` over the unsatisfied groups gives AND across types. A requested
type the memory does not carry imposes nothing — the "extra types in the
query" rule. The ``q`` side is ``COLLATE "C"`` to match the columns'
byte-oriented collation, so identity here is the identity the primary key uses.
"""

RRF_QUERY_TEMPLATE = """
WITH semantic_search AS (
    SELECT
        id,
        RANK() OVER (ORDER BY embedding::vector({dim}) <=> %(embedding)s::vector({dim})) AS rank
    FROM memories
    WHERE deleted_at IS NULL
      AND tenant = %(tenant)s
      {{filters}}
    ORDER BY embedding::vector({dim}) <=> %(embedding)s::vector({dim})
    LIMIT 20
),
keyword_search AS (
    SELECT
        id,
        RANK() OVER (
            ORDER BY ts_rank_cd(tsv, plainto_tsquery('english', %(query)s)) DESC
        ) AS rank
    FROM memories
    WHERE deleted_at IS NULL
      AND tenant = %(tenant)s
      AND tsv @@ plainto_tsquery('english', %(query)s)
      {{filters}}
    ORDER BY ts_rank_cd(tsv, plainto_tsquery('english', %(query)s)) DESC
    LIMIT 20
)
SELECT
    m.id,
    m.content,
    m.summary,
    m.type,
    m.subtype,
    m.domain,
    m.tenant,
    m.depth_layer,
    m.decay_score,
    m.trust_score,
    m.metadata,
    m.origin,
    m.access_count,
    m.created_at,
    m.accessed_at,
    m.hrr_vector,
    COALESCE(1.0 / (%(rrf_k)s + s.rank), 0.0) +
    COALESCE(1.0 / (%(rrf_k)s + k.rank), 0.0) AS rrf_score,
    m.decay_score * m.trust_score * CASE m.depth_layer
        WHEN 'core' THEN 1.5
        WHEN 'stable' THEN 1.2
        WHEN 'working' THEN 1.0
        WHEN 'ephemeral' THEN 0.5
        ELSE 1.0
    END AS quality_weight
FROM memories m
LEFT JOIN semantic_search s ON m.id = s.id
LEFT JOIN keyword_search k ON m.id = k.id
WHERE (s.id IS NOT NULL OR k.id IS NOT NULL)
ORDER BY
    (COALESCE(1.0 / (%(rrf_k)s + s.rank), 0.0) +
     COALESCE(1.0 / (%(rrf_k)s + k.rank), 0.0)) *
    m.decay_score * m.trust_score * CASE m.depth_layer
        WHEN 'core' THEN 1.5
        WHEN 'stable' THEN 1.2
        WHEN 'working' THEN 1.0
        WHEN 'ephemeral' THEN 0.5
        ELSE 1.0
    END DESC
LIMIT %(limit)s;
"""

VECTOR_ONLY_TEMPLATE = """
SELECT
    id, content, summary, type, subtype, domain, tenant, depth_layer, decay_score, trust_score, metadata,
    origin,
    access_count, created_at, accessed_at,
    1 - (embedding::vector({dim}) <=> %(embedding)s::vector({dim})) AS similarity
FROM memories
WHERE deleted_at IS NULL
  AND tenant = %(tenant)s
  {{filters}}
ORDER BY embedding::vector({dim}) <=> %(embedding)s::vector({dim})
LIMIT %(limit)s;
"""

COUNT = """
SELECT count(*) AS total
FROM memories
WHERE deleted_at IS NULL
  AND tenant = %(tenant)s
  {filters}
"""

FILTER_DEPTH_LAYER = "AND depth_layer = %(depth_layer)s"

FILTER_SUBTYPE = "AND subtype = %(subtype)s"

FILTER_DOMAIN = "AND domain = %(domain)s"

FILTER_ORIGIN = "AND origin = %(origin)s"

FILTER_METADATA = "AND metadata @> %(metadata_filter)s::jsonb"

SCOPE_FILTER = """AND EXISTS (
          SELECT 1 FROM memory_scopes any_scope
          WHERE any_scope.memory_id = memories.id
      )
      AND (
          EXISTS (
              SELECT 1 FROM memory_scopes global_scope
              WHERE global_scope.memory_id = memories.id
                AND global_scope.scope_type = %(global_type)s
                AND global_scope.scope_key = %(global_key)s
          )
          OR NOT EXISTS (
              SELECT 1
              FROM memory_scopes ms
              LEFT JOIN unnest(%(scope_types)s::text[], %(scope_keys)s::text[])
                     AS q(scope_type, scope_key)
                ON q.scope_type COLLATE "C" = ms.scope_type
               AND q.scope_key COLLATE "C" = ms.scope_key
              WHERE ms.memory_id = memories.id
              GROUP BY ms.scope_type
              HAVING count(q.scope_key) = 0
          )
      )"""
