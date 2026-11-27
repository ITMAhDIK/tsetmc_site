-- Bind signup consent to the OTP challenge that initiated registration.
-- Verification cannot switch to a different legal text version after the SMS
-- has already been issued.
BEGIN;

ALTER TABLE phone_verifications
  ADD COLUMN IF NOT EXISTS signup_terms_version VARCHAR(80),
  ADD COLUMN IF NOT EXISTS signup_privacy_version VARCHAR(80);

CREATE INDEX IF NOT EXISTS idx_phone_verifications_signup_versions
  ON phone_verifications(phone_e164,purpose,created_at DESC)
  WHERE purpose='signup';

COMMIT;
