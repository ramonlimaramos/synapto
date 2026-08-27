-- migrate:up

CREATE TABLE IF NOT EXISTS metrics_events (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('counter', 'gauge', 'histogram')),
    value DOUBLE PRECISION NOT NULL,
    tags JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Composite index drives `synapto metrics summary --tool X --since 1h` (T8).
CREATE INDEX IF NOT EXISTS idx_metrics_events_name_created
    ON metrics_events (name, created_at DESC, id DESC);

-- Standalone index drives the retention purge: `DELETE WHERE created_at < ...`
-- has no name predicate and cannot use the composite above efficiently.
CREATE INDEX IF NOT EXISTS idx_metrics_events_created_at
    ON metrics_events (created_at);

-- migrate:down

DROP INDEX IF EXISTS idx_metrics_events_created_at;
DROP INDEX IF EXISTS idx_metrics_events_name_created;
DROP TABLE IF EXISTS metrics_events CASCADE;
