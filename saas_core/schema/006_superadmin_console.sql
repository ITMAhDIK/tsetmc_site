-- Phase 8: superadmin console and payment reconciliation notes.
-- This migration targets only the isolated SaaS PostgreSQL database.
CREATE TABLE IF NOT EXISTS payment_reconciliation_notes (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
 order_id UUID NOT NULL REFERENCES payment_orders(id) ON DELETE RESTRICT,
 actor_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 observed_status VARCHAR(16) NOT NULL,
 note TEXT NOT NULL CHECK(length(note) BETWEEN 3 AND 2000),
 idempotency_key VARCHAR(120) NOT NULL UNIQUE,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_reconciliation_order_time
 ON payment_reconciliation_notes(order_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reconciliation_actor_time
 ON payment_reconciliation_notes(actor_user_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_audit_target_time
 ON admin_audit_logs(target_type,target_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_users_status_created
 ON users(status,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_users_username_lower
 ON users(lower(username));
CREATE INDEX IF NOT EXISTS idx_payment_attempts_provider_status
 ON payment_attempts(provider,status,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_withdrawals_status_requested
 ON withdrawal_requests(status,requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_referrals_status_created
 ON referrals(status,created_at DESC);

INSERT INTO permissions(code,description)
VALUES ('superadmin.console.access','Access the superadmin console')
ON CONFLICT(code) DO NOTHING;
INSERT INTO role_permissions(role_id,permission_id)
SELECT r.id,p.id FROM roles r CROSS JOIN permissions p
WHERE r.code='superadmin' AND p.code='superadmin.console.access'
ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION reject_reconciliation_note_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'payment_reconciliation_notes are append-only';
END;
$$;
DROP TRIGGER IF EXISTS trg_reconciliation_notes_immutable ON payment_reconciliation_notes;
CREATE TRIGGER trg_reconciliation_notes_immutable
 BEFORE UPDATE OR DELETE ON payment_reconciliation_notes
 FOR EACH ROW EXECUTE FUNCTION reject_reconciliation_note_mutation();
