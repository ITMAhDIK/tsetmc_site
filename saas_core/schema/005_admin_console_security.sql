-- Phase 8: secure staff sessions and immutable administrative evidence.
-- Apply only to the dedicated SaaS PostgreSQL database, after migrations 001–004.
BEGIN;

-- Staff console sessions are created only by the trusted authentication flow.
-- MFA is recorded by the server after a second-factor challenge, never by the browser.
ALTER TABLE sessions
  ADD COLUMN IF NOT EXISTS mfa_verified_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS csrf_token_hash CHAR(64);

CREATE INDEX IF NOT EXISTS idx_sessions_active_user_expiry
  ON sessions(user_id, expires_at)
  WHERE revoked_at IS NULL;

CREATE OR REPLACE FUNCTION reject_admin_evidence_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION '% is append-only; create a new compensating record', TG_TABLE_NAME;
END;
$$;

DROP TRIGGER IF EXISTS trg_admin_audit_immutable ON admin_audit_logs;
CREATE TRIGGER trg_admin_audit_immutable
  BEFORE UPDATE OR DELETE ON admin_audit_logs
  FOR EACH ROW EXECUTE FUNCTION reject_admin_evidence_mutation();

DROP TRIGGER IF EXISTS trg_admin_payment_reviews_immutable ON admin_payment_reviews;
CREATE TRIGGER trg_admin_payment_reviews_immutable
  BEFORE UPDATE OR DELETE ON admin_payment_reviews
  FOR EACH ROW EXECUTE FUNCTION reject_admin_evidence_mutation();

COMMIT;
