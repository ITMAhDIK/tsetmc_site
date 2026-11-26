-- Phase 8: superadmin console permissions. SaaS database only.
INSERT INTO permissions(code,description) VALUES
 ('admin.console.access','Access the superadmin console overview'),
 ('referrals.read','View referral attribution and rewards')
ON CONFLICT(code) DO NOTHING;
INSERT INTO role_permissions(role_id,permission_id)
 SELECT r.id,p.id FROM roles r CROSS JOIN permissions p
 WHERE r.code='superadmin' AND p.code IN ('admin.console.access','referrals.read')
ON CONFLICT DO NOTHING;
