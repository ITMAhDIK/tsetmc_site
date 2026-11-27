-- Snapshot the purchased duration so catalogue edits never mutate existing orders.
ALTER TABLE payment_orders ADD COLUMN IF NOT EXISTS plan_duration_days INTEGER;
UPDATE payment_orders AS o SET plan_duration_days=p.duration_days
FROM plans AS p WHERE p.id=o.plan_id AND o.plan_duration_days IS NULL;
ALTER TABLE payment_orders ALTER COLUMN plan_duration_days SET NOT NULL;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_commerce_payment_order_plan_duration'
                 AND conrelid='public.payment_orders'::regclass) THEN
    ALTER TABLE payment_orders ADD CONSTRAINT ck_commerce_payment_order_plan_duration
      CHECK (plan_duration_days > 0);
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS wallet_order_payments (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id UUID NOT NULL UNIQUE REFERENCES payment_orders(id) ON DELETE RESTRICT,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  amount_toman BIGINT NOT NULL CHECK (amount_toman > 0),
  gift_debit_toman BIGINT NOT NULL DEFAULT 0 CHECK (gift_debit_toman >= 0),
  cash_debit_toman BIGINT NOT NULL DEFAULT 0 CHECK (cash_debit_toman >= 0),
  gift_ledger_entry_id UUID UNIQUE REFERENCES wallet_ledger_entries(id) ON DELETE RESTRICT,
  cash_ledger_entry_id UUID UNIQUE REFERENCES wallet_ledger_entries(id) ON DELETE RESTRICT,
  idempotency_key VARCHAR(120) NOT NULL UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (gift_debit_toman + cash_debit_toman = amount_toman),
  CHECK ((gift_debit_toman = 0 AND gift_ledger_entry_id IS NULL) OR
         (gift_debit_toman > 0 AND gift_ledger_entry_id IS NOT NULL)),
  CHECK ((cash_debit_toman = 0 AND cash_ledger_entry_id IS NULL) OR
         (cash_debit_toman > 0 AND cash_ledger_entry_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_commerce_wallet_order_payments_user_created
  ON wallet_order_payments(user_id,created_at DESC);

CREATE OR REPLACE FUNCTION reject_commerce_wallet_order_payment_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
  RAISE EXCEPTION 'wallet_order_payments is append-only';
END; $$;
DROP TRIGGER IF EXISTS trg_commerce_wallet_order_payments_immutable ON wallet_order_payments;
CREATE TRIGGER trg_commerce_wallet_order_payments_immutable
  BEFORE UPDATE OR DELETE ON wallet_order_payments
  FOR EACH ROW EXECUTE FUNCTION reject_commerce_wallet_order_payment_mutation();
