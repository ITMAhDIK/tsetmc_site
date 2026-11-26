-- OTP delivery worker leasing, bounded retries and redacted failure status.
-- The SMS worker claims rows with SKIP LOCKED and uses a short lease so a
-- crashed worker cannot permanently strand a notification.
ALTER TABLE notification_outbox
 ADD COLUMN IF NOT EXISTS locked_at TIMESTAMPTZ,
 ADD COLUMN IF NOT EXISTS last_error VARCHAR(120);

CREATE INDEX IF NOT EXISTS idx_notification_sms_due
 ON notification_outbox(next_attempt_at,created_at)
 WHERE channel='sms' AND template_code='phone_otp' AND status='pending';
