-- Privileged account MFA and immutable legal-version acceptance evidence.
BEGIN;

ALTER TABLE sessions
  ADD COLUMN IF NOT EXISTS mfa_verified_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_sessions_user_mfa
  ON sessions(user_id,mfa_verified_at DESC)
  WHERE revoked_at IS NULL;

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
CREATE INDEX IF NOT EXISTS idx_legal_acceptances_user_time
  ON legal_acceptances(user_id,accepted_at DESC);

COMMIT;
