-- Commerce Service schema v1. Subscription, payment and wallet data only.
-- User IDs are opaque UUIDs owned by Identity; there are no cross-database FKs.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS users (
  id UUID PRIMARY KEY,
  status VARCHAR(16) NOT NULL CHECK (status IN ('active','suspended','deleted','pending')),
  phone_e164 VARCHAR(20),
  phone_verified_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (phone_e164 IS NULL OR phone_e164 ~ '^\+[1-9][0-9]{7,14}
);

CREATE TABLE IF NOT EXISTS plans (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  code VARCHAR(32) NOT NULL UNIQUE,
  title VARCHAR(100) NOT NULL,
  duration_days INTEGER NOT NULL CHECK (duration_days > 0),
  price_toman BIGINT NOT NULL CHECK (price_toman > 0),
  currency VARCHAR(8) NOT NULL DEFAULT 'toman' CHECK (currency='toman'),
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO plans(code,title,duration_days,price_toman) VALUES
 ('premium_monthly','اشتراک ویژه یک‌ماهه',30,899000),
 ('premium_quarterly','اشتراک ویژه سه‌ماهه',90,2499000)
ON CONFLICT(code) DO NOTHING;

CREATE TABLE IF NOT EXISTS payment_orders (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  plan_id UUID NOT NULL REFERENCES plans(id) ON DELETE RESTRICT,
  amount_toman BIGINT NOT NULL CHECK (amount_toman > 0),
  status VARCHAR(16) NOT NULL DEFAULT 'created'
    CHECK (status IN ('created','pending','paid','failed','cancelled','refunded')),
  idempotency_key VARCHAR(100) NOT NULL UNIQUE,
  phone_e164 VARCHAR(20) NOT NULL,
  currency VARCHAR(8) NOT NULL DEFAULT 'toman' CHECK (currency='toman'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ,
  paid_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_commerce_orders_user_created
  ON payment_orders(user_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_commerce_orders_pending_expiry
  ON payment_orders(status,expires_at) WHERE status IN ('created','pending');

CREATE TABLE IF NOT EXISTS payment_attempts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id UUID NOT NULL REFERENCES payment_orders(id) ON DELETE RESTRICT,
  provider VARCHAR(20) NOT NULL CHECK (provider IN ('snappay','digipay')),
  provider_order_id VARCHAR(160),
  redirect_url TEXT,
  amount_toman BIGINT NOT NULL CHECK (amount_toman > 0),
  currency VARCHAR(8) NOT NULL DEFAULT 'toman' CHECK (currency='toman'),
  status VARCHAR(20) NOT NULL DEFAULT 'initiated',
  request_ref VARCHAR(160),
  failure_code VARCHAR(80),
  failure_message VARCHAR(300),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  verified_at TIMESTAMPTZ,
  settled_at TIMESTAMPTZ,
  UNIQUE(provider,provider_order_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_commerce_payment_attempt_one_active_per_order
  ON payment_attempts(order_id) WHERE status IN ('initiated','pending');
CREATE INDEX IF NOT EXISTS idx_commerce_attempts_order_created
  ON payment_attempts(order_id,created_at DESC);

CREATE TABLE IF NOT EXISTS gateway_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  provider VARCHAR(20) NOT NULL,
  provider_event_id VARCHAR(200) NOT NULL,
  order_id UUID REFERENCES payment_orders(id) ON DELETE RESTRICT,
  event_type VARCHAR(80) NOT NULL,
  payload_sha256 CHAR(64) NOT NULL,
  received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  processed_at TIMESTAMPTZ,
  processing_status VARCHAR(16) NOT NULL DEFAULT 'received'
    CHECK (processing_status IN ('received','processed','rejected','failed')),
  UNIQUE(provider,provider_event_id)
);

CREATE TABLE IF NOT EXISTS subscriptions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  plan_id UUID NOT NULL REFERENCES plans(id) ON DELETE RESTRICT,
  status VARCHAR(16) NOT NULL
    CHECK (status IN ('pending','active','expired','cancelled','refunded')),
  starts_at TIMESTAMPTZ,
  ends_at TIMESTAMPTZ,
  payment_order_id UUID REFERENCES payment_orders(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (ends_at IS NULL OR starts_at IS NULL OR ends_at > starts_at)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_commerce_subscription_payment_order
  ON subscriptions(payment_order_id) WHERE payment_order_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_commerce_subscriptions_user_status
  ON subscriptions(user_id,status,ends_at DESC);

CREATE TABLE IF NOT EXISTS payment_refunds (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id UUID NOT NULL REFERENCES payment_orders(id) ON DELETE RESTRICT,
  payment_attempt_id UUID NOT NULL REFERENCES payment_attempts(id) ON DELETE RESTRICT,
  amount_toman BIGINT NOT NULL CHECK (amount_toman > 0),
  status VARCHAR(16) NOT NULL DEFAULT 'requested'
    CHECK (status IN ('requested','pending','succeeded','failed','cancelled')),
  idempotency_key VARCHAR(120) NOT NULL UNIQUE,
  provider_refund_id VARCHAR(160) UNIQUE,
  reason VARCHAR(300),
  requested_by UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_commerce_refunds_order ON payment_refunds(order_id,created_at DESC);

CREATE TABLE IF NOT EXISTS wallet_accounts (
  user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS wallet_ledger_entries (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES wallet_accounts(user_id) ON DELETE RESTRICT,
  bucket VARCHAR(8) NOT NULL CHECK (bucket IN ('cash','gift')),
  entry_type VARCHAR(24) NOT NULL CHECK (entry_type IN
    ('topup','referral_reward','purchase_debit','withdrawal_debit','refund','admin_credit','admin_debit','reversal')),
  amount_toman BIGINT NOT NULL CHECK (amount_toman <> 0),
  reference_type VARCHAR(32),
  reference_id UUID,
  idempotency_key VARCHAR(120) NOT NULL UNIQUE,
  description TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_commerce_ledger_user_time ON wallet_ledger_entries(user_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_commerce_ledger_user_bucket ON wallet_ledger_entries(user_id,bucket,created_at);

CREATE TABLE IF NOT EXISTS wallet_bank_accounts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  bank_name VARCHAR(100),
  account_fingerprint CHAR(64) NOT NULL,
  encrypted_account_token TEXT NOT NULL,
  verification_status VARCHAR(16) NOT NULL DEFAULT 'pending'
    CHECK (verification_status IN ('pending','verified','rejected')),
  verified_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(user_id,account_fingerprint),
  UNIQUE(id,user_id)
);
CREATE TABLE IF NOT EXISTS withdrawal_requests (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  amount_toman BIGINT NOT NULL CHECK (amount_toman > 0),
  status VARCHAR(16) NOT NULL DEFAULT 'requested'
    CHECK (status IN ('requested','approved','rejected','paid','cancelled')),
  bank_account_token TEXT NOT NULL,
  bank_account_id UUID,
  idempotency_key VARCHAR(120),
  requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  reviewed_at TIMESTAMPTZ,
  reviewed_by UUID REFERENCES users(id) ON DELETE RESTRICT,
  paid_at TIMESTAMPTZ,
  review_note TEXT,
  CONSTRAINT commerce_withdrawal_bank_owner_fk
    FOREIGN KEY(bank_account_id,user_id) REFERENCES wallet_bank_accounts(id,user_id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_commerce_withdrawal_idempotency
  ON withdrawal_requests(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_commerce_withdrawals_user_status
  ON withdrawal_requests(user_id,status,requested_at DESC);

CREATE TABLE IF NOT EXISTS referral_codes (
  user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE RESTRICT,
  code VARCHAR(24) NOT NULL UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS referrals (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  inviter_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  referred_user_id UUID NOT NULL UNIQUE REFERENCES users(id) ON DELETE RESTRICT,
  status VARCHAR(16) NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','qualified','rewarded','rejected')),
  qualified_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (inviter_user_id <> referred_user_id)
);
CREATE TABLE IF NOT EXISTS referral_rewards (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  referral_id UUID NOT NULL UNIQUE REFERENCES referrals(id) ON DELETE RESTRICT,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  amount_toman BIGINT NOT NULL DEFAULT 100000 CHECK (amount_toman > 0),
  ledger_entry_id UUID UNIQUE REFERENCES wallet_ledger_entries(id) ON DELETE RESTRICT,
  sms_status VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK (sms_status IN ('pending','sent','failed')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_commerce_referrals_inviter_status
  ON referrals(inviter_user_id,status,created_at DESC);

CREATE OR REPLACE FUNCTION reject_commerce_ledger_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'wallet_ledger_entries is append-only; add a compensating entry';
END;
$$;
DROP TRIGGER IF EXISTS trg_commerce_wallet_ledger_immutable ON wallet_ledger_entries;
CREATE TRIGGER trg_commerce_wallet_ledger_immutable
  BEFORE UPDATE OR DELETE ON wallet_ledger_entries
  FOR EACH ROW EXECUTE FUNCTION reject_commerce_ledger_mutation();

CREATE OR REPLACE VIEW wallet_balances AS
SELECT user_id,
  COALESCE(SUM(amount_toman) FILTER (WHERE bucket='cash'),0)::BIGINT AS cash_balance_toman,
  COALESCE(SUM(amount_toman) FILTER (WHERE bucket='gift'),0)::BIGINT AS gift_balance_toman,
  COALESCE(SUM(amount_toman),0)::BIGINT AS total_balance_toman
FROM wallet_ledger_entries GROUP BY user_id;

CREATE OR REPLACE VIEW wallet_withdrawable_balances AS
SELECT b.user_id,b.cash_balance_toman,
  COALESCE(r.reserved_toman,0)::BIGINT AS reserved_toman,
  GREATEST(b.cash_balance_toman-COALESCE(r.reserved_toman,0),0)::BIGINT AS available_to_withdraw_toman
FROM wallet_balances b LEFT JOIN (
  SELECT user_id,SUM(amount_toman) AS reserved_toman
  FROM withdrawal_requests WHERE status IN ('requested','approved') GROUP BY user_id
) r ON r.user_id=b.user_id;
)
);

CREATE TABLE IF NOT EXISTS plans (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  code VARCHAR(32) NOT NULL UNIQUE,
  title VARCHAR(100) NOT NULL,
  duration_days INTEGER NOT NULL CHECK (duration_days > 0),
  price_toman BIGINT NOT NULL CHECK (price_toman > 0),
  currency VARCHAR(8) NOT NULL DEFAULT 'toman' CHECK (currency='toman'),
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO plans(code,title,duration_days,price_toman) VALUES
 ('premium_monthly','اشتراک ویژه یک‌ماهه',30,899000),
 ('premium_quarterly','اشتراک ویژه سه‌ماهه',90,2499000)
ON CONFLICT(code) DO NOTHING;

CREATE TABLE IF NOT EXISTS payment_orders (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  plan_id UUID NOT NULL REFERENCES plans(id) ON DELETE RESTRICT,
  amount_toman BIGINT NOT NULL CHECK (amount_toman > 0),
  status VARCHAR(16) NOT NULL DEFAULT 'created'
    CHECK (status IN ('created','pending','paid','failed','cancelled','refunded')),
  idempotency_key VARCHAR(100) NOT NULL UNIQUE,
  phone_e164 VARCHAR(20) NOT NULL,
  currency VARCHAR(8) NOT NULL DEFAULT 'toman' CHECK (currency='toman'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ,
  paid_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_commerce_orders_user_created
  ON payment_orders(user_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_commerce_orders_pending_expiry
  ON payment_orders(status,expires_at) WHERE status IN ('created','pending');

CREATE TABLE IF NOT EXISTS payment_attempts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id UUID NOT NULL REFERENCES payment_orders(id) ON DELETE RESTRICT,
  provider VARCHAR(20) NOT NULL CHECK (provider IN ('snappay','digipay')),
  provider_order_id VARCHAR(160),
  redirect_url TEXT,
  amount_toman BIGINT NOT NULL CHECK (amount_toman > 0),
  currency VARCHAR(8) NOT NULL DEFAULT 'toman' CHECK (currency='toman'),
  status VARCHAR(20) NOT NULL DEFAULT 'initiated',
  request_ref VARCHAR(160),
  failure_code VARCHAR(80),
  failure_message VARCHAR(300),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  verified_at TIMESTAMPTZ,
  settled_at TIMESTAMPTZ,
  UNIQUE(provider,provider_order_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_commerce_payment_attempt_one_active_per_order
  ON payment_attempts(order_id) WHERE status IN ('initiated','pending');
CREATE INDEX IF NOT EXISTS idx_commerce_attempts_order_created
  ON payment_attempts(order_id,created_at DESC);

CREATE TABLE IF NOT EXISTS gateway_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  provider VARCHAR(20) NOT NULL,
  provider_event_id VARCHAR(200) NOT NULL,
  order_id UUID REFERENCES payment_orders(id) ON DELETE RESTRICT,
  event_type VARCHAR(80) NOT NULL,
  payload_sha256 CHAR(64) NOT NULL,
  received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  processed_at TIMESTAMPTZ,
  processing_status VARCHAR(16) NOT NULL DEFAULT 'received'
    CHECK (processing_status IN ('received','processed','rejected','failed')),
  UNIQUE(provider,provider_event_id)
);

CREATE TABLE IF NOT EXISTS subscriptions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  plan_id UUID NOT NULL REFERENCES plans(id) ON DELETE RESTRICT,
  status VARCHAR(16) NOT NULL
    CHECK (status IN ('pending','active','expired','cancelled','refunded')),
  starts_at TIMESTAMPTZ,
  ends_at TIMESTAMPTZ,
  payment_order_id UUID REFERENCES payment_orders(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (ends_at IS NULL OR starts_at IS NULL OR ends_at > starts_at)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_commerce_subscription_payment_order
  ON subscriptions(payment_order_id) WHERE payment_order_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_commerce_subscriptions_user_status
  ON subscriptions(user_id,status,ends_at DESC);

CREATE TABLE IF NOT EXISTS payment_refunds (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id UUID NOT NULL REFERENCES payment_orders(id) ON DELETE RESTRICT,
  payment_attempt_id UUID NOT NULL REFERENCES payment_attempts(id) ON DELETE RESTRICT,
  amount_toman BIGINT NOT NULL CHECK (amount_toman > 0),
  status VARCHAR(16) NOT NULL DEFAULT 'requested'
    CHECK (status IN ('requested','pending','succeeded','failed','cancelled')),
  idempotency_key VARCHAR(120) NOT NULL UNIQUE,
  provider_refund_id VARCHAR(160) UNIQUE,
  reason VARCHAR(300),
  requested_by UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_commerce_refunds_order ON payment_refunds(order_id,created_at DESC);

CREATE TABLE IF NOT EXISTS wallet_accounts (
  user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS wallet_ledger_entries (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES wallet_accounts(user_id) ON DELETE RESTRICT,
  bucket VARCHAR(8) NOT NULL CHECK (bucket IN ('cash','gift')),
  entry_type VARCHAR(24) NOT NULL CHECK (entry_type IN
    ('topup','referral_reward','purchase_debit','withdrawal_debit','refund','admin_credit','admin_debit','reversal')),
  amount_toman BIGINT NOT NULL CHECK (amount_toman <> 0),
  reference_type VARCHAR(32),
  reference_id UUID,
  idempotency_key VARCHAR(120) NOT NULL UNIQUE,
  description TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_commerce_ledger_user_time ON wallet_ledger_entries(user_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_commerce_ledger_user_bucket ON wallet_ledger_entries(user_id,bucket,created_at);

CREATE TABLE IF NOT EXISTS wallet_bank_accounts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  bank_name VARCHAR(100),
  account_fingerprint CHAR(64) NOT NULL,
  encrypted_account_token TEXT NOT NULL,
  verification_status VARCHAR(16) NOT NULL DEFAULT 'pending'
    CHECK (verification_status IN ('pending','verified','rejected')),
  verified_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(user_id,account_fingerprint),
  UNIQUE(id,user_id)
);
CREATE TABLE IF NOT EXISTS withdrawal_requests (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  amount_toman BIGINT NOT NULL CHECK (amount_toman > 0),
  status VARCHAR(16) NOT NULL DEFAULT 'requested'
    CHECK (status IN ('requested','approved','rejected','paid','cancelled')),
  bank_account_token TEXT NOT NULL,
  bank_account_id UUID,
  idempotency_key VARCHAR(120),
  requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  reviewed_at TIMESTAMPTZ,
  reviewed_by UUID REFERENCES users(id) ON DELETE RESTRICT,
  paid_at TIMESTAMPTZ,
  review_note TEXT,
  CONSTRAINT commerce_withdrawal_bank_owner_fk
    FOREIGN KEY(bank_account_id,user_id) REFERENCES wallet_bank_accounts(id,user_id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_commerce_withdrawal_idempotency
  ON withdrawal_requests(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_commerce_withdrawals_user_status
  ON withdrawal_requests(user_id,status,requested_at DESC);

CREATE TABLE IF NOT EXISTS referral_codes (
  user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE RESTRICT,
  code VARCHAR(24) NOT NULL UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS referrals (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  inviter_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  referred_user_id UUID NOT NULL UNIQUE REFERENCES users(id) ON DELETE RESTRICT,
  status VARCHAR(16) NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','qualified','rewarded','rejected')),
  qualified_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (inviter_user_id <> referred_user_id)
);
CREATE TABLE IF NOT EXISTS referral_rewards (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  referral_id UUID NOT NULL UNIQUE REFERENCES referrals(id) ON DELETE RESTRICT,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  amount_toman BIGINT NOT NULL DEFAULT 100000 CHECK (amount_toman > 0),
  ledger_entry_id UUID UNIQUE REFERENCES wallet_ledger_entries(id) ON DELETE RESTRICT,
  sms_status VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK (sms_status IN ('pending','sent','failed')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_commerce_referrals_inviter_status
  ON referrals(inviter_user_id,status,created_at DESC);

CREATE OR REPLACE FUNCTION reject_commerce_ledger_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'wallet_ledger_entries is append-only; add a compensating entry';
END;
$$;
DROP TRIGGER IF EXISTS trg_commerce_wallet_ledger_immutable ON wallet_ledger_entries;
CREATE TRIGGER trg_commerce_wallet_ledger_immutable
  BEFORE UPDATE OR DELETE ON wallet_ledger_entries
  FOR EACH ROW EXECUTE FUNCTION reject_commerce_ledger_mutation();

CREATE OR REPLACE VIEW wallet_balances AS
SELECT user_id,
  COALESCE(SUM(amount_toman) FILTER (WHERE bucket='cash'),0)::BIGINT AS cash_balance_toman,
  COALESCE(SUM(amount_toman) FILTER (WHERE bucket='gift'),0)::BIGINT AS gift_balance_toman,
  COALESCE(SUM(amount_toman),0)::BIGINT AS total_balance_toman
FROM wallet_ledger_entries GROUP BY user_id;

CREATE OR REPLACE VIEW wallet_withdrawable_balances AS
SELECT b.user_id,b.cash_balance_toman,
  COALESCE(r.reserved_toman,0)::BIGINT AS reserved_toman,
  GREATEST(b.cash_balance_toman-COALESCE(r.reserved_toman,0),0)::BIGINT AS available_to_withdraw_toman
FROM wallet_balances b LEFT JOIN (
  SELECT user_id,SUM(amount_toman) AS reserved_toman
  FROM withdrawal_requests WHERE status IN ('requested','approved') GROUP BY user_id
) r ON r.user_id=b.user_id;
