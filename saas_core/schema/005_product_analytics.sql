-- Phase 7 product analytics: privacy, deduplication, and aggregate-query indexes.
-- Existing consent/event tables are defined in 001_initial.sql.
ALTER TABLE analytics_events
 ADD COLUMN IF NOT EXISTS client_event_id UUID;
ALTER TABLE analytics_events
 ADD COLUMN IF NOT EXISTS x_pct NUMERIC(5,2)
 CHECK (x_pct IS NULL OR x_pct BETWEEN 0 AND 100);
ALTER TABLE analytics_events
 ADD COLUMN IF NOT EXISTS y_pct NUMERIC(5,2)
 CHECK (y_pct IS NULL OR y_pct BETWEEN 0 AND 100);

CREATE UNIQUE INDEX IF NOT EXISTS uq_analytics_user_client_event
 ON analytics_events(user_id,client_event_id)
 WHERE user_id IS NOT NULL AND client_event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_analytics_page_event_time
 ON analytics_events(page_path,event_name,occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_analytics_event_user_time
 ON analytics_events(event_name,user_id,occurred_at DESC)
 WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_analytics_heatmap
 ON analytics_events(page_path,event_name,x_pct,y_pct,occurred_at DESC)
 WHERE x_pct IS NOT NULL AND y_pct IS NOT NULL;

-- Individual event histories contain user-level behavioral data. Keep this
-- permission separate from aggregate analytics.read and grant it only to superadmins.
INSERT INTO permissions(code,description)
VALUES ('analytics.users.read','View consented user-level product analytics')
ON CONFLICT(code) DO NOTHING;

INSERT INTO role_permissions(role_id,permission_id)
SELECT r.id,p.id
FROM roles r CROSS JOIN permissions p
WHERE r.code='superadmin' AND p.code='analytics.users.read'
ON CONFLICT DO NOTHING;

INSERT INTO permissions(code,description)
VALUES ('analytics.retention.manage','Run product analytics retention cleanup')
ON CONFLICT(code) DO NOTHING;

INSERT INTO role_permissions(role_id,permission_id)
SELECT r.id,p.id
FROM roles r CROSS JOIN permissions p
WHERE r.code='superadmin' AND p.code='analytics.retention.manage'
ON CONFLICT DO NOTHING;
