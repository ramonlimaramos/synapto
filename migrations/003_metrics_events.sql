-- migrate:up

CREATE TABLE IF NOT EXISTS metrics_events (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    type VARCHAR(16) NOT NULL CHECK (type IN ('counter', 'gauge', 'histogram')),
    value DOUBLE PRECISION NOT NULL,
    tags JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_metrics_events_name_created
    ON metrics_events (name, created_at DESC);

-- migrate:down

DROP INDEX IF EXISTS idx_metrics_events_name_created;
DROP TABLE IF EXISTS metrics_events CASCADE;
