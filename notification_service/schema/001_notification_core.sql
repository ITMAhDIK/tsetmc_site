CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE IF NOT EXISTS notification_preferences (
 user_id UUID PRIMARY KEY,
 email_enabled BOOLEAN NOT NULL DEFAULT TRUE,
 sms_enabled BOOLEAN NOT NULL DEFAULT TRUE,
 in_app_enabled BOOLEAN NOT NULL DEFAULT TRUE,
 marketing_enabled BOOLEAN NOT NULL DEFAULT FALSE,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS notifications (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
 user_id UUID NOT NULL,
 template_key VARCHAR(64) NOT NULL,
 payload JSONB NOT NULL DEFAULT '{}'::jsonb,
 category VARCHAR(24) NOT NULL CHECK(category IN ('security','transactional','product','marketing')),
 in_app_visible BOOLEAN NOT NULL DEFAULT TRUE,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 expires_at TIMESTAMPTZ,
 read_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_notifications_inbox ON notifications(user_id,created_at DESC,id);
CREATE TABLE IF NOT EXISTS notification_deliveries (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
 notification_id UUID NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
 channel VARCHAR(12) NOT NULL CHECK(channel IN ('email','sms','in_app')),
 destination_hash CHAR(64),
 status VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','processing','sent','retry','dead','skipped')),
 attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
 max_attempts INTEGER NOT NULL DEFAULT 6 CHECK(max_attempts BETWEEN 1 AND 12),
 next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 locked_at TIMESTAMPTZ,
 lock_token UUID,
 provider_message_id VARCHAR(180),
 last_error_code VARCHAR(80),
 sent_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(notification_id,channel)
);
CREATE INDEX IF NOT EXISTS idx_notification_delivery_queue ON notification_deliveries(next_attempt_at,created_at) WHERE status IN ('pending','retry');
CREATE INDEX IF NOT EXISTS idx_notification_delivery_lease ON notification_deliveries(locked_at) WHERE status='processing';
CREATE TABLE IF NOT EXISTS notification_idempotency (
 idempotency_key VARCHAR(160) PRIMARY KEY,
 request_hash CHAR(64) NOT NULL,
 notification_ids UUID[] NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 expires_at TIMESTAMPTZ NOT NULL DEFAULT now()+interval '7 days'
);
CREATE INDEX IF NOT EXISTS idx_notification_idempotency_expiry ON notification_idempotency(expires_at);
CREATE TABLE IF NOT EXISTS notification_audit_log (
 id BIGSERIAL PRIMARY KEY,
 actor VARCHAR(80) NOT NULL,
 action VARCHAR(80) NOT NULL,
 notification_id UUID,
 metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE OR REPLACE FUNCTION notification_delivery_transition_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.status='sent' AND NEW.status<>'sent' THEN RAISE EXCEPTION 'sent notification deliveries are immutable'; END IF;
 IF OLD.status='dead' AND NEW.status NOT IN ('dead','pending') THEN RAISE EXCEPTION 'dead deliveries require explicit replay'; END IF;
 RETURN NEW;
END; $$;
DROP TRIGGER IF EXISTS trg_notification_delivery_transition ON notification_deliveries;
CREATE TRIGGER trg_notification_delivery_transition BEFORE UPDATE ON notification_deliveries FOR EACH ROW EXECUTE FUNCTION notification_delivery_transition_guard();
