CREATE TABLE IF NOT EXISTS crm_customers (
  user_id UUID PRIMARY KEY,
  username VARCHAR(80) NOT NULL,
  display_name VARCHAR(160) NOT NULL DEFAULT '',
  account_status VARCHAR(16) NOT NULL CHECK (account_status IN ('active','suspended','pending','deleted')),
  phone_verified BOOLEAN NOT NULL DEFAULT FALSE,
  email_verified BOOLEAN NOT NULL DEFAULT FALSE,
  lifecycle_stage VARCHAR(16) NOT NULL DEFAULT 'new'
    CHECK (lifecycle_stage IN ('new','active','at_risk','churned')),
  assigned_to UUID,
  created_at TIMESTAMPTZ NOT NULL,
  projected_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_crm_customers_search ON crm_customers (created_at DESC, username);
CREATE INDEX IF NOT EXISTS idx_crm_customers_assignee ON crm_customers (assigned_to, lifecycle_stage);
CREATE TABLE IF NOT EXISTS crm_interactions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id UUID NOT NULL REFERENCES crm_customers(user_id) ON DELETE RESTRICT,
  actor_user_id UUID NOT NULL,
  channel VARCHAR(16) NOT NULL CHECK (channel IN ('call','email','meeting','note','support','other')),
  subject VARCHAR(180) NOT NULL CHECK (length(btrim(subject)) BETWEEN 3 AND 180),
  summary TEXT NOT NULL CHECK (length(btrim(summary)) BETWEEN 2 AND 4000),
  happened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_crm_interactions_customer_time ON crm_interactions(customer_id,happened_at DESC,created_at DESC);
CREATE OR REPLACE FUNCTION crm_reject_interaction_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'CRM interactions are immutable'; END; $$;
DROP TRIGGER IF EXISTS trg_crm_interactions_immutable ON crm_interactions;
CREATE TRIGGER trg_crm_interactions_immutable BEFORE UPDATE OR DELETE ON crm_interactions
FOR EACH ROW EXECUTE FUNCTION crm_reject_interaction_mutation();
CREATE TABLE IF NOT EXISTS crm_followups (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id UUID NOT NULL REFERENCES crm_customers(user_id) ON DELETE RESTRICT,
  assigned_to UUID NOT NULL,
  created_by UUID NOT NULL,
  title VARCHAR(180) NOT NULL CHECK (length(btrim(title)) BETWEEN 3 AND 180),
  details TEXT NOT NULL DEFAULT '' CHECK (length(details)<=2000),
  due_at TIMESTAMPTZ NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'open' CHECK(status IN ('open','completed','cancelled')),
  completed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK ((status='completed')=(completed_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_crm_followups_due ON crm_followups(status,due_at,assigned_to);
CREATE TABLE IF NOT EXISTS crm_audit_log (
  id BIGSERIAL PRIMARY KEY,
  actor_user_id UUID NOT NULL,
  action VARCHAR(80) NOT NULL,
  target_type VARCHAR(40) NOT NULL,
  target_id VARCHAR(80) NOT NULL,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_crm_audit_time ON crm_audit_log(created_at DESC);
