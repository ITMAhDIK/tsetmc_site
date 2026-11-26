-- Phase 7: consent-aware, first-party analytics. SaaS database only.
ALTER TABLE analytics_events ADD COLUMN IF NOT EXISTS client_event_id UUID;
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
