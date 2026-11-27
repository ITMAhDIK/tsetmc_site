-- Phase 9: immutable administrator audit history.
-- This migration applies only to the dedicated SaaS PostgreSQL database.
CREATE OR REPLACE FUNCTION reject_admin_audit_log_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'admin_audit_logs are append-only; create a corrective audit event';
END;
$$;

DROP TRIGGER IF EXISTS trg_admin_audit_logs_immutable ON admin_audit_logs;
CREATE TRIGGER trg_admin_audit_logs_immutable
    BEFORE UPDATE OR DELETE ON admin_audit_logs
    FOR EACH ROW EXECUTE FUNCTION reject_admin_audit_log_mutation();
