-- Product analytics ingestion hardening.
-- Apply only to the dedicated SaaS PostgreSQL database, after migration 002.
BEGIN;

-- Anonymous visitors need a current consent record as well as append-only history.
CREATE TABLE IF NOT EXISTS analytics_anonymous_consents (
  anonymous_id UUID PRIMARY KEY,
  product_analytics BOOLEAN NOT NULL DEFAULT FALSE,
  consent_version VARCHAR(24) NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Client-generated event IDs make retried batches idempotent. NULL remains
-- allowed so pre-migration rows and trusted server-side records are preserved.
ALTER TABLE analytics_events
  ADD COLUMN IF NOT EXISTS event_id UUID;

CREATE UNIQUE INDEX IF NOT EXISTS idx_analytics_events_event_id_unique
  ON analytics_events(event_id)
  WHERE event_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_analytics_events_retention
  ON analytics_events(occurred_at);

COMMIT;
