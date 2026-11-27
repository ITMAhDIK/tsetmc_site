-- Identity Service owns credentials, sessions, OTP, MFA, legal evidence and RBAC.
-- This database is deliberately independent from both Market and SaaS databases.
BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

CREATE TABLE IF NOT EXISTS users (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
 public_id VARCHAR(32) NOT NULL UNIQUE,
 username VARCHAR(64) UNIQUE,
 display_name VARCHAR(160),
 account_type VARCHAR(16) NOT NULL DEFAULT 'standard' CHECK(account_type IN ('standard','special')),
 status VARCHAR(16) NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended','deleted','pending')),
 phone_e164 VARCHAR(20) UNIQUE,
 phone_verified_at TIMESTAMPTZ,
 email CITEXT UNIQUE,
 email_verified_at TIMESTAMPTZ,
 password_hash TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 last_login_at TIMESTAMPTZ,
 CHECK(phone_e164 IS NULL OR phone_e164 ~ '^\+[1-9][0-9]{7,14}$')
);

CREATE TABLE IF NOT EXISTS auth_identities (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
 user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 provider VARCHAR(24) NOT NULL CHECK(provider IN ('password','google','phone_otp')),
 provider_subject VARCHAR(255) NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 last_used_at TIMESTAMPTZ,
 UNIQUE(provider,provider_subject)
);

CREATE TABLE IF NOT EXISTS email_verifications (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
 user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 token_hash TEXT NOT NULL,
 expires_at TIMESTAMPTZ NOT NULL,
 consumed_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS phone_verifications (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
 phone_e164 VARCHAR(20) NOT NULL,
 purpose VARCHAR(24) NOT NULL CHECK(purpose IN ('signup','login','purchase','link_identity','withdrawal')),
 otp_hash TEXT NOT NULL,
 attempts SMALLINT NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 10),
 expires_at TIMESTAMPTZ NOT NULL,
 consumed_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 signup_terms_version VARCHAR(80),
 signup_privacy_version VARCHAR(80)
);
CREATE INDEX IF NOT EXISTS idx_identity_phone_challenge
 ON phone_verifications(phone_e164,purpose,created_at DESC)
 WHERE consumed_at IS NULL;

CREATE TABLE IF NOT EXISTS sessions (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
 user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 token_hash TEXT NOT NULL UNIQUE,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 expires_at TIMESTAMPTZ NOT NULL,
 revoked_at TIMESTAMPTZ,
 ip_hash TEXT,
 user_agent TEXT,
 mfa_verified_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_identity_sessions_user_active
 ON sessions(user_id,last_seen_at DESC) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS roles (
 id SMALLSERIAL PRIMARY KEY,
 code VARCHAR(32) NOT NULL UNIQUE,
 title VARCHAR(80) NOT NULL
);
INSERT INTO roles(code,title) VALUES
 ('superadmin','Super Administrator'),('admin','Administrator'),
 ('support','Support Agent'),('user','User')
ON CONFLICT(code) DO NOTHING;

CREATE TABLE IF NOT EXISTS permissions (
 id SMALLSERIAL PRIMARY KEY,
 code VARCHAR(80) NOT NULL UNIQUE,
 description TEXT NOT NULL
);
INSERT INTO permissions(code,description) VALUES
 ('users.read','View user accounts'),('users.manage','Manage user accounts'),
 ('roles.manage','Assign administrative roles'),('market.admin','Manage market data operations'),
 ('subscriptions.manage','Manage subscriptions'),('payments.read','View payment records'),
 ('wallet.read','View wallet ledgers'),('wallet.adjust','Create audited wallet adjustments'),
 ('withdrawals.review','Review withdrawal requests'),('tickets.manage','Manage support tickets'),
 ('analytics.read','View product analytics'),('audit.read','View administrative audit logs'),
 ('faq.manage','Manage FAQ content'),('superadmin.console.access','Access the superadmin console'),
 ('analytics.users.read','View consented user-level product analytics'),
 ('analytics.retention.manage','Run product analytics retention cleanup'),
 ('crm.read','Read customer relationship records and follow-ups'),
 ('crm.manage','Create and manage customer interactions and follow-ups')
ON CONFLICT(code) DO NOTHING;

CREATE TABLE IF NOT EXISTS role_permissions (
 role_id SMALLINT NOT NULL REFERENCES roles(id) ON DELETE RESTRICT,
 permission_id SMALLINT NOT NULL REFERENCES permissions(id) ON DELETE RESTRICT,
 PRIMARY KEY(role_id,permission_id)
);
INSERT INTO role_permissions(role_id,permission_id)
 SELECT r.id,p.id FROM roles r CROSS JOIN permissions p
 WHERE r.code='superadmin' ON CONFLICT DO NOTHING;
INSERT INTO role_permissions(role_id,permission_id)
 SELECT r.id,p.id FROM roles r JOIN permissions p ON p.code IN
 ('users.read','subscriptions.manage','payments.read','wallet.read','withdrawals.review',
  'tickets.manage','analytics.read','audit.read','faq.manage','crm.read','crm.manage')
 WHERE r.code='admin' ON CONFLICT DO NOTHING;
INSERT INTO role_permissions(role_id,permission_id)
 SELECT r.id,p.id FROM roles r JOIN permissions p ON p.code='tickets.manage'
 WHERE r.code='support' ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS user_roles (
 user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 role_id SMALLINT NOT NULL REFERENCES roles(id) ON DELETE RESTRICT,
 granted_by UUID REFERENCES users(id) ON DELETE RESTRICT,
 granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(user_id,role_id)
);

CREATE TABLE IF NOT EXISTS user_admin_mfa (
 user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE RESTRICT,
 encrypted_secret TEXT NOT NULL,
 enabled_at TIMESTAMPTZ,
 last_verified_counter BIGINT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS legal_acceptances (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
 user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 terms_version VARCHAR(80) NOT NULL,
 privacy_version VARCHAR(80) NOT NULL,
 accepted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 source VARCHAR(24) NOT NULL CHECK(source IN ('phone_otp_signup','password_signup')),
 UNIQUE(user_id,terms_version,privacy_version)
);
CREATE INDEX IF NOT EXISTS idx_identity_legal_acceptances_user_time
 ON legal_acceptances(user_id,accepted_at DESC);

CREATE TABLE IF NOT EXISTS notification_outbox (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
 user_id UUID REFERENCES users(id) ON DELETE RESTRICT,
 channel VARCHAR(12) NOT NULL CHECK(channel IN ('sms','email')),
 template_code VARCHAR(80) NOT NULL,
 destination_token TEXT NOT NULL,
 payload JSONB NOT NULL DEFAULT '{}'::jsonb,
 idempotency_key VARCHAR(140) NOT NULL UNIQUE,
 status VARCHAR(12) NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sent','failed')),
 attempts SMALLINT NOT NULL DEFAULT 0,
 next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 sent_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 locked_at TIMESTAMPTZ,
 last_error VARCHAR(120)
);
CREATE INDEX IF NOT EXISTS idx_identity_sms_due
 ON notification_outbox(next_attempt_at,created_at)
 WHERE channel='sms' AND template_code='phone_otp' AND status='pending';

CREATE TABLE IF NOT EXISTS admin_audit_logs (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
 actor_user_id UUID REFERENCES users(id) ON DELETE RESTRICT,
 action VARCHAR(100) NOT NULL,
 target_type VARCHAR(60),
 target_id TEXT,
 request_id VARCHAR(80),
 ip_hash TEXT,
 before_state JSONB,
 after_state JSONB,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_identity_audit_time ON admin_audit_logs(created_at DESC);

CREATE OR REPLACE FUNCTION reject_identity_immutable_evidence()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'identity legal acceptance evidence is append-only';
END;
$$;
DROP TRIGGER IF EXISTS trg_identity_legal_immutable ON legal_acceptances;
CREATE TRIGGER trg_identity_legal_immutable BEFORE UPDATE OR DELETE ON legal_acceptances
 FOR EACH ROW EXECUTE FUNCTION reject_identity_immutable_evidence();
