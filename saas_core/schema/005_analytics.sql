-- Phase 7: consent-aware, first-party analytics. SaaS database only.
ALTER TABLE analytics_events ADD COLUMN IF NOT EXISTS client_event_id UUID;
ALTER TABLE analytics_events ADD COLUMN IF NOT EXISTS consent_version VARCHAR(24);
ALTER TABLE analytics_events ADD COLUMN IF NOT EXISTS payload_sha256 CHAR(64);
CREATE UNIQUE INDEX IF NOT EXISTS uq_analytics_user_client_event
 ON analytics_events(user_id,client_event_id) WHERE client_event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_analytics_page_time
 ON analytics_events(event_name,page_path,occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_analytics_funnel_time
 ON analytics_events((metadata->>'funnel_name'),occurred_at DESC)
 WHERE event_name='funnel_step';
INSERT INTO permissions(code,description)
 VALUES ('analytics.users.read','View an individual user product activity')
 ON CONFLICT(code) DO NOTHING;
INSERT INTO role_permissions(role_id,permission_id)
 SELECT r.id,p.id FROM roles r CROSS JOIN permissions p
 WHERE r.code='superadmin' AND p.code='analytics.users.read'
 ON CONFLICT DO NOTHING;


-- Preserve an immutable record of each actual consent change/version acceptance.
CREATE TABLE IF NOT EXISTS analytics_consent_history (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
 user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 product_analytics BOOLEAN NOT NULL,
 consent_version VARCHAR(24) NOT NULL,
 changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_analytics_consent_history_user_time
 ON analytics_consent_history(user_id,changed_at DESC);
CREATE OR REPLACE FUNCTION reject_analytics_consent_history_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'analytics_consent_history is append-only'; END;
$$;
DROP TRIGGER IF EXISTS trg_analytics_consent_history_immutable ON analytics_consent_history;
CREATE TRIGGER trg_analytics_consent_history_immutable
 BEFORE UPDATE OR DELETE ON analytics_consent_history
 FOR EACH ROW EXECUTE FUNCTION reject_analytics_consent_history_mutation();
