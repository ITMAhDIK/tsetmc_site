-- Phase 4 payment lifecycle. Idempotent and isolated to SaaS PostgreSQL.
ALTER TABLE payment_orders
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS currency VARCHAR(8) NOT NULL DEFAULT 'toman';

ALTER TABLE payment_attempts
    ADD COLUMN IF NOT EXISTS amount_toman BIGINT,
    ADD COLUMN IF NOT EXISTS currency VARCHAR(8) NOT NULL DEFAULT 'toman',
    ADD COLUMN IF NOT EXISTS failure_code VARCHAR(80),
    ADD COLUMN IF NOT EXISTS failure_message VARCHAR(300);

ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS payment_order_id UUID;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='payment_orders_currency_toman_only') THEN
    ALTER TABLE payment_orders ADD CONSTRAINT payment_orders_currency_toman_only CHECK(currency='toman');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='payment_attempts_amount_positive') THEN
    ALTER TABLE payment_attempts ADD CONSTRAINT payment_attempts_amount_positive
      CHECK(amount_toman IS NULL OR amount_toman>0);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='payment_attempts_currency_toman_only') THEN
    ALTER TABLE payment_attempts ADD CONSTRAINT payment_attempts_currency_toman_only CHECK(currency='toman');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='subscriptions_payment_order_fk') THEN
    ALTER TABLE subscriptions ADD CONSTRAINT subscriptions_payment_order_fk
      FOREIGN KEY(payment_order_id) REFERENCES payment_orders(id) ON DELETE RESTRICT;
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_subscriptions_payment_order
 ON subscriptions(payment_order_id) WHERE payment_order_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_payment_orders_user_created ON payment_orders(user_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_payment_orders_pending_expiry ON payment_orders(status,expires_at)
 WHERE status IN ('created','pending');
CREATE INDEX IF NOT EXISTS idx_payment_attempts_order_created ON payment_attempts(order_id,created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_attempt_one_active_per_order ON payment_attempts(order_id)
 WHERE status IN ('initiated','pending');

CREATE TABLE IF NOT EXISTS payment_refunds (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
 order_id UUID NOT NULL REFERENCES payment_orders(id) ON DELETE RESTRICT,
 payment_attempt_id UUID NOT NULL REFERENCES payment_attempts(id) ON DELETE RESTRICT,
 amount_toman BIGINT NOT NULL CHECK(amount_toman>0),
 status VARCHAR(16) NOT NULL DEFAULT 'requested'
   CHECK(status IN ('requested','pending','succeeded','failed','cancelled')),
 idempotency_key VARCHAR(120) NOT NULL UNIQUE,
 provider_refund_id VARCHAR(160) UNIQUE,
 reason VARCHAR(300),
 requested_by UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_payment_refunds_order ON payment_refunds(order_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_payment_refunds_queue ON payment_refunds(status,created_at)
 WHERE status='requested';

CREATE TABLE IF NOT EXISTS payment_reconciliation_cases (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
 order_id UUID NOT NULL REFERENCES payment_orders(id) ON DELETE RESTRICT,
 payment_attempt_id UUID NOT NULL REFERENCES payment_attempts(id) ON DELETE RESTRICT,
 reason VARCHAR(80) NOT NULL,
 status VARCHAR(16) NOT NULL DEFAULT 'open'
   CHECK(status IN ('open','resolved','dismissed')),
 resolution_note VARCHAR(500),
 resolved_by UUID REFERENCES users(id) ON DELETE RESTRICT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 resolved_at TIMESTAMPTZ,
 UNIQUE(payment_attempt_id,reason)
);
CREATE INDEX IF NOT EXISTS idx_payment_reconciliation_open
 ON payment_reconciliation_cases(status,created_at) WHERE status='open';
