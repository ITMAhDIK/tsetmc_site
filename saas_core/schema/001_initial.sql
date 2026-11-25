-- Tloomberg SaaS Core schema v1. All money is integer Toman.
-- This database is independent from the market-data databases.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

CREATE TABLE IF NOT EXISTS users (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(), public_id VARCHAR(32) NOT NULL UNIQUE,
 username VARCHAR(64) UNIQUE, display_name VARCHAR(160),
 account_type VARCHAR(16) NOT NULL DEFAULT 'standard' CHECK(account_type IN ('standard','special')),
 status VARCHAR(16) NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended','deleted','pending')),
 phone_e164 VARCHAR(20) UNIQUE, phone_verified_at TIMESTAMPTZ,
 email CITEXT UNIQUE, email_verified_at TIMESTAMPTZ, password_hash TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 last_login_at TIMESTAMPTZ,
 CHECK(phone_e164 IS NULL OR phone_e164 ~ '^\+[1-9][0-9]{7,14}$')
);
CREATE TABLE IF NOT EXISTS auth_identities (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(), user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 provider VARCHAR(24) NOT NULL CHECK(provider IN ('password','google','phone_otp')),
 provider_subject VARCHAR(255) NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 last_used_at TIMESTAMPTZ, UNIQUE(provider,provider_subject)
);
CREATE TABLE IF NOT EXISTS phone_verifications (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(), phone_e164 VARCHAR(20) NOT NULL,
 purpose VARCHAR(24) NOT NULL CHECK(purpose IN ('signup','login','purchase','link_identity','withdrawal')),
 otp_hash TEXT NOT NULL, attempts SMALLINT NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 10),
 expires_at TIMESTAMPTZ NOT NULL, consumed_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS email_verifications (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(), user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 token_hash TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL, consumed_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS sessions (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(), user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 token_hash TEXT NOT NULL UNIQUE, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(), expires_at TIMESTAMPTZ NOT NULL,
 revoked_at TIMESTAMPTZ, ip_hash TEXT, user_agent TEXT
);
CREATE TABLE IF NOT EXISTS roles(id SMALLSERIAL PRIMARY KEY, code VARCHAR(32) NOT NULL UNIQUE, title VARCHAR(80) NOT NULL);
INSERT INTO roles(code,title) VALUES ('superadmin','Super Administrator'),('admin','Administrator'),('support','Support Agent'),('user','User') ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS permissions(id SMALLSERIAL PRIMARY KEY, code VARCHAR(80) NOT NULL UNIQUE, description TEXT NOT NULL);
INSERT INTO permissions(code,description) VALUES
 ('users.read','View user accounts'),('users.manage','Manage user accounts'),('roles.manage','Assign administrative roles'),
 ('market.admin','Manage market data operations'),('subscriptions.manage','Manage subscriptions'),('payments.read','View payment records'),
 ('wallet.read','View wallet ledgers'),('wallet.adjust','Create audited wallet adjustments'),('withdrawals.review','Review withdrawal requests'),
 ('tickets.manage','Manage support tickets'),('analytics.read','View product analytics'),('audit.read','View administrative audit logs'),
 ('faq.manage','Manage FAQ content')
ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS role_permissions(
 role_id SMALLINT NOT NULL REFERENCES roles(id) ON DELETE RESTRICT,
 permission_id SMALLINT NOT NULL REFERENCES permissions(id) ON DELETE RESTRICT, PRIMARY KEY(role_id,permission_id)
);
INSERT INTO role_permissions(role_id,permission_id)
 SELECT r.id,p.id FROM roles r CROSS JOIN permissions p WHERE r.code='superadmin' ON CONFLICT DO NOTHING;
INSERT INTO role_permissions(role_id,permission_id)
 SELECT r.id,p.id FROM roles r JOIN permissions p ON p.code IN ('users.read','subscriptions.manage','payments.read','wallet.read','withdrawals.review','tickets.manage','analytics.read','audit.read','faq.manage')
 WHERE r.code='admin' ON CONFLICT DO NOTHING;
INSERT INTO role_permissions(role_id,permission_id)
 SELECT r.id,p.id FROM roles r JOIN permissions p ON p.code IN ('tickets.manage')
 WHERE r.code='support' ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS user_roles(
 user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT, role_id SMALLINT NOT NULL REFERENCES roles(id) ON DELETE RESTRICT,
 granted_by UUID REFERENCES users(id) ON DELETE RESTRICT, granted_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(user_id,role_id)
);
CREATE TABLE IF NOT EXISTS user_preferences(
 user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE RESTRICT,
 theme VARCHAR(8) NOT NULL DEFAULT 'system' CHECK(theme IN ('light','dark','system')),
 calendar VARCHAR(10) NOT NULL DEFAULT 'jalali' CHECK(calendar IN ('jalali','gregorian')),
 currency VARCHAR(8) NOT NULL DEFAULT 'toman' CHECK(currency IN ('toman','rial')),
 locale VARCHAR(12) NOT NULL DEFAULT 'fa-IR', updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS plans(
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(), code VARCHAR(32) NOT NULL UNIQUE, title VARCHAR(100) NOT NULL,
 duration_days INTEGER NOT NULL CHECK(duration_days>0), price_toman BIGINT NOT NULL CHECK(price_toman>=0),
 active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO plans(code,title,duration_days,price_toman) VALUES
 ('premium_monthly','اشتراک ویژه یک‌ماهه',30,899000),('premium_quarterly','اشتراک ویژه سه‌ماهه',90,2499000)
ON CONFLICT(code) DO NOTHING;
CREATE TABLE IF NOT EXISTS subscriptions(
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(), user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 plan_id UUID NOT NULL REFERENCES plans(id) ON DELETE RESTRICT,
 status VARCHAR(16) NOT NULL CHECK(status IN ('pending','active','expired','cancelled','refunded')),
 starts_at TIMESTAMPTZ, ends_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 CHECK(ends_at IS NULL OR starts_at IS NULL OR ends_at>starts_at)
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_user_status ON subscriptions(user_id,status,ends_at DESC);
CREATE TABLE IF NOT EXISTS payment_orders(
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(), user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 plan_id UUID REFERENCES plans(id) ON DELETE RESTRICT, amount_toman BIGINT NOT NULL CHECK(amount_toman>0),
 status VARCHAR(16) NOT NULL DEFAULT 'created' CHECK(status IN ('created','pending','paid','failed','cancelled','refunded')),
 idempotency_key VARCHAR(100) NOT NULL UNIQUE, phone_e164 VARCHAR(20) NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), paid_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS payment_attempts(
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(), order_id UUID NOT NULL REFERENCES payment_orders(id) ON DELETE RESTRICT,
 provider VARCHAR(20) NOT NULL CHECK(provider IN ('snappay','digipay')), provider_order_id VARCHAR(160),
 redirect_url TEXT, status VARCHAR(20) NOT NULL DEFAULT 'initiated', request_ref VARCHAR(160),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), verified_at TIMESTAMPTZ, settled_at TIMESTAMPTZ,
 UNIQUE(provider,provider_order_id)
);
CREATE TABLE IF NOT EXISTS gateway_events(
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(), provider VARCHAR(20) NOT NULL, provider_event_id VARCHAR(200) NOT NULL,
 order_id UUID REFERENCES payment_orders(id) ON DELETE RESTRICT, event_type VARCHAR(80) NOT NULL,
 payload_sha256 CHAR(64) NOT NULL, received_at TIMESTAMPTZ NOT NULL DEFAULT now(), processed_at TIMESTAMPTZ,
 processing_status VARCHAR(16) NOT NULL DEFAULT 'received' CHECK(processing_status IN ('received','processed','rejected','failed')),
 UNIQUE(provider,provider_event_id)
);
CREATE TABLE IF NOT EXISTS wallet_accounts(user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE RESTRICT, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS wallet_ledger_entries(
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(), user_id UUID NOT NULL REFERENCES wallet_accounts(user_id) ON DELETE RESTRICT,
 bucket VARCHAR(8) NOT NULL CHECK(bucket IN ('cash','gift')),
 entry_type VARCHAR(24) NOT NULL CHECK(entry_type IN ('topup','referral_reward','purchase_debit','withdrawal_debit','refund','admin_credit','admin_debit','reversal')),
 amount_toman BIGINT NOT NULL CHECK(amount_toman<>0), reference_type VARCHAR(32), reference_id UUID,
 idempotency_key VARCHAR(120) NOT NULL UNIQUE, description TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_wallet_ledger_user_time ON wallet_ledger_entries(user_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_wallet_ledger_user_bucket ON wallet_ledger_entries(user_id,bucket,created_at);
CREATE TABLE IF NOT EXISTS withdrawal_requests(
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(), user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 amount_toman BIGINT NOT NULL CHECK(amount_toman>0),
 status VARCHAR(16) NOT NULL DEFAULT 'requested' CHECK(status IN ('requested','approved','rejected','paid','cancelled')),
 bank_account_token TEXT NOT NULL, requested_at TIMESTAMPTZ NOT NULL DEFAULT now(), reviewed_at TIMESTAMPTZ,
 reviewed_by UUID REFERENCES users(id) ON DELETE RESTRICT, paid_at TIMESTAMPTZ, review_note TEXT
);
CREATE TABLE IF NOT EXISTS referral_codes(
 user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE RESTRICT, code VARCHAR(24) NOT NULL UNIQUE, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS referrals(
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(), inviter_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 referred_user_id UUID NOT NULL UNIQUE REFERENCES users(id) ON DELETE RESTRICT,
 status VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','qualified','rewarded','rejected')),
 qualified_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), CHECK(inviter_user_id<>referred_user_id)
);
CREATE TABLE IF NOT EXISTS referral_rewards(
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(), referral_id UUID NOT NULL UNIQUE REFERENCES referrals(id) ON DELETE RESTRICT,
 user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT, amount_toman BIGINT NOT NULL DEFAULT 100000 CHECK(amount_toman>0),
 ledger_entry_id UUID UNIQUE REFERENCES wallet_ledger_entries(id) ON DELETE RESTRICT,
 sms_status VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK(sms_status IN ('pending','sent','failed')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS tickets(
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(), ticket_number BIGSERIAL UNIQUE,
 user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT, assigned_to UUID REFERENCES users(id) ON DELETE RESTRICT,
 subject VARCHAR(180) NOT NULL, category VARCHAR(40),
 priority VARCHAR(12) NOT NULL DEFAULT 'normal' CHECK(priority IN ('low','normal','high','urgent')),
 status VARCHAR(16) NOT NULL DEFAULT 'open' CHECK(status IN ('open','waiting','answered','closed')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ticket_messages(
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(), ticket_id UUID NOT NULL REFERENCES tickets(id) ON DELETE RESTRICT,
 sender_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT, body TEXT NOT NULL,
 is_internal_note BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS faq_articles(
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(), slug VARCHAR(120) NOT NULL UNIQUE, question VARCHAR(300) NOT NULL,
 answer TEXT NOT NULL, sort_order INTEGER NOT NULL DEFAULT 0, published BOOLEAN NOT NULL DEFAULT FALSE,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS analytics_consents(
 user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE RESTRICT, essential BOOLEAN NOT NULL DEFAULT TRUE,
 product_analytics BOOLEAN NOT NULL DEFAULT FALSE, consent_version VARCHAR(24) NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS analytics_events(
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(), user_id UUID REFERENCES users(id) ON DELETE SET NULL,
 anonymous_id UUID, session_id UUID, event_name VARCHAR(64) NOT NULL, page_path VARCHAR(300) NOT NULL,
 component_id VARCHAR(160), element_role VARCHAR(40), occurred_at TIMESTAMPTZ NOT NULL,
 received_at TIMESTAMPTZ NOT NULL DEFAULT now(), viewport_width INTEGER, viewport_height INTEGER,
 scroll_depth_pct NUMERIC(5,2) CHECK(scroll_depth_pct BETWEEN 0 AND 100),
 active_duration_ms INTEGER CHECK(active_duration_ms IS NULL OR active_duration_ms>=0),
 metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_analytics_user_time ON analytics_events(user_id,occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_analytics_session_time ON analytics_events(session_id,occurred_at);
CREATE INDEX IF NOT EXISTS idx_analytics_event_time ON analytics_events(event_name,occurred_at DESC);
CREATE TABLE IF NOT EXISTS admin_audit_logs(
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(), actor_user_id UUID REFERENCES users(id) ON DELETE RESTRICT,
 action VARCHAR(100) NOT NULL, target_type VARCHAR(60), target_id TEXT, request_id VARCHAR(80),
 ip_hash TEXT, before_state JSONB, after_state JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_admin_audit_time ON admin_audit_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_audit_actor ON admin_audit_logs(actor_user_id,created_at DESC);

CREATE OR REPLACE FUNCTION reject_wallet_ledger_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'wallet_ledger_entries is append-only; add a compensating entry'; END;
$$;
DROP TRIGGER IF EXISTS trg_wallet_ledger_immutable ON wallet_ledger_entries;
CREATE TRIGGER trg_wallet_ledger_immutable BEFORE UPDATE OR DELETE ON wallet_ledger_entries
FOR EACH ROW EXECUTE FUNCTION reject_wallet_ledger_mutation();

CREATE OR REPLACE VIEW wallet_balances AS
SELECT user_id,
 COALESCE(SUM(amount_toman) FILTER(WHERE bucket='cash'),0)::BIGINT AS cash_balance_toman,
 COALESCE(SUM(amount_toman) FILTER(WHERE bucket='gift'),0)::BIGINT AS gift_balance_toman,
 COALESCE(SUM(amount_toman),0)::BIGINT AS total_balance_toman
FROM wallet_ledger_entries GROUP BY user_id;

-- App services must lock rows and atomically reserve pending withdrawals,
-- verify/settle provider payments and post all ledger entries with idempotency.
