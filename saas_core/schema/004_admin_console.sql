-- Phase 8: administrative console schema additions, SaaS database only.
BEGIN;
INSERT INTO permissions(code,description) VALUES
 ('admin.console.read','View administrative console overview'),
 ('payments.reconcile','Record payment reconciliation reviews'),
 ('referrals.read','View referral activity')
ON CONFLICT(code) DO NOTHING;

INSERT INTO role_permissions(role_id,permission_id)
SELECT r.id,p.id FROM roles r CROSS JOIN permissions p
WHERE r.code='superadmin' ON CONFLICT DO NOTHING;

INSERT INTO role_permissions(role_id,permission_id)
SELECT r.id,p.id FROM roles r JOIN permissions p ON p.code IN (
 'admin.console.read','users.read','users.manage','subscriptions.manage',
 'payments.read','payments.reconcile','wallet.read','wallet.adjust',
 'withdrawals.review','referrals.read','tickets.manage','faq.manage',
 'analytics.read','audit.read'
) WHERE r.code='admin' ON CONFLICT DO NOTHING;

ALTER TABLE withdrawal_requests
 ADD COLUMN IF NOT EXISTS payout_reference VARCHAR(200);

CREATE TABLE IF NOT EXISTS admin_payment_reviews (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
 order_id UUID NOT NULL REFERENCES payment_orders(id) ON DELETE RESTRICT,
 actor_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 outcome VARCHAR(24) NOT NULL CHECK(outcome IN ('matched','mismatch','provider_followup','blocked')),
 note TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_admin_payment_reviews_order_time
 ON admin_payment_reviews(order_id,created_at DESC);
COMMIT;
