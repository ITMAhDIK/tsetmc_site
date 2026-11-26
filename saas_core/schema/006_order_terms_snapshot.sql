-- Snapshot subscription duration at order creation, alongside the already-captured price.
ALTER TABLE payment_orders ADD COLUMN IF NOT EXISTS duration_days INTEGER;
UPDATE payment_orders o SET duration_days=p.duration_days
FROM plans p WHERE o.plan_id=p.id AND o.duration_days IS NULL;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='payment_orders_duration_days_positive') THEN
  ALTER TABLE payment_orders ADD CONSTRAINT payment_orders_duration_days_positive CHECK(duration_days>0);
 END IF;
END $$;
ALTER TABLE payment_orders ALTER COLUMN duration_days SET NOT NULL;
