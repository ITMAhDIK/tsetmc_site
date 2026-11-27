-- Phase 7: Product Analytics owns consent and behavioral-event persistence.
-- Deliberately no cross-database foreign keys to Identity or SaaS users.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SEQUENCE IF NOT EXISTS analytics_event_order_seq AS BIGINT;

CREATE TABLE IF NOT EXISTS analytics_consents (
    user_id UUID PRIMARY KEY,
    essential BOOLEAN NOT NULL DEFAULT TRUE,
    product_analytics BOOLEAN NOT NULL DEFAULT FALSE,
    consent_version VARCHAR(24) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS analytics_consent_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    essential BOOLEAN NOT NULL DEFAULT TRUE,
    product_analytics BOOLEAN NOT NULL,
    consent_version VARCHAR(24) NOT NULL,
    source VARCHAR(24) NOT NULL DEFAULT 'api'
      CHECK (source IN ('api','legacy_import')),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_analytics_consent_history_user_time
    ON analytics_consent_history(user_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS analytics_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    anonymous_id UUID,
    session_id UUID NOT NULL,
    event_name VARCHAR(64) NOT NULL CHECK (event_name IN
      ('page_view','click','navigation','scroll','active_time','feature_view')),
    page_path VARCHAR(300) NOT NULL,
    component_id VARCHAR(80),
    element_role VARCHAR(80),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    viewport_width INTEGER CHECK (viewport_width BETWEEN 1 AND 10000),
    viewport_height INTEGER CHECK (viewport_height BETWEEN 1 AND 10000),
    scroll_depth_pct NUMERIC(5,2) CHECK (scroll_depth_pct BETWEEN 0 AND 100),
    active_duration_ms INTEGER CHECK (active_duration_ms BETWEEN 0 AND 180000),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    client_event_id UUID NOT NULL,
    x_pct NUMERIC(5,2) CHECK (x_pct BETWEEN 0 AND 100),
    y_pct NUMERIC(5,2) CHECK (y_pct BETWEEN 0 AND 100),
    event_order BIGINT NOT NULL DEFAULT nextval('analytics_event_order_seq'),
    CHECK ((x_pct IS NULL) = (y_pct IS NULL))
);
ALTER SEQUENCE analytics_event_order_seq OWNED BY analytics_events.event_order;
CREATE UNIQUE INDEX IF NOT EXISTS uq_analytics_user_client_event
    ON analytics_events(user_id, client_event_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_analytics_event_order
    ON analytics_events(event_order);
CREATE INDEX IF NOT EXISTS idx_analytics_page_event_time
    ON analytics_events(page_path,event_name,occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_analytics_event_user_time
    ON analytics_events(event_name,user_id,occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_analytics_event_retention_time
    ON analytics_events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_analytics_heatmap
    ON analytics_events(page_path,event_name,x_pct,y_pct,occurred_at DESC)
    WHERE x_pct IS NOT NULL AND y_pct IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_analytics_path_sequence_session
    ON analytics_events(user_id,session_id,event_order,id)
    INCLUDE(page_path) WHERE event_name='page_view';
CREATE INDEX IF NOT EXISTS idx_analytics_path_sequence_time
    ON analytics_events(occurred_at)
    INCLUDE(user_id,session_id,page_path,id)
    WHERE event_name='page_view';

CREATE TABLE IF NOT EXISTS analytics_ingestion_limits (
    user_id UUID PRIMARY KEY,
    window_started_at TIMESTAMPTZ NOT NULL,
    event_count INTEGER NOT NULL CHECK (event_count >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_analytics_ingestion_limit_window
    ON analytics_ingestion_limits(window_started_at);

CREATE TABLE IF NOT EXISTS analytics_audit_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_user_id UUID,
    action VARCHAR(100) NOT NULL,
    target_type VARCHAR(60),
    target_id TEXT,
    request_id VARCHAR(80),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_analytics_audit_actor_time
    ON analytics_audit_logs(actor_user_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_analytics_audit_time
    ON analytics_audit_logs(created_at DESC);

CREATE OR REPLACE FUNCTION reject_analytics_evidence_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Analytics consent history and audit logs are append-only';
END;
$$;
DROP TRIGGER IF EXISTS trg_analytics_consent_history_immutable ON analytics_consent_history;
CREATE TRIGGER trg_analytics_consent_history_immutable
    BEFORE UPDATE OR DELETE ON analytics_consent_history
    FOR EACH ROW EXECUTE FUNCTION reject_analytics_evidence_mutation();
DROP TRIGGER IF EXISTS trg_analytics_audit_logs_immutable ON analytics_audit_logs;
CREATE TRIGGER trg_analytics_audit_logs_immutable
    BEFORE UPDATE OR DELETE ON analytics_audit_logs
    FOR EACH ROW EXECUTE FUNCTION reject_analytics_evidence_mutation();
