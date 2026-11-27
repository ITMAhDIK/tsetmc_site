-- Phase 10: internal customer relationship management foundation.
-- CRM references stable SaaS user IDs only; it does not replicate contact
-- details or create any dependency on the Market database.
BEGIN;

INSERT INTO permissions(code,description) VALUES
 ('crm.read','Read customer relationship records and follow-ups'),
 ('crm.manage','Create and manage customer interactions and follow-ups')
ON CONFLICT(code) DO NOTHING;

-- CRM is restricted to admin/superadmin staff. Support agents retain ticket
-- access only; a support role is not automatically granted CRM visibility.
INSERT INTO role_permissions(role_id,permission_id)
SELECT r.id,p.id
FROM roles r
CROSS JOIN permissions p
WHERE r.code IN ('superadmin','admin')
  AND p.code IN ('crm.read','crm.manage')
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS crm_customer_profiles (
  user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE RESTRICT,
  lifecycle_stage VARCHAR(16) NOT NULL DEFAULT 'new'
    CHECK (lifecycle_stage IN ('new','active','at_risk','churned')),
  assigned_to UUID REFERENCES users(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS crm_interactions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  actor_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  channel VARCHAR(16) NOT NULL
    CHECK (channel IN ('call','email','meeting','note','support','other')),
  subject VARCHAR(180) NOT NULL CHECK (length(btrim(subject)) BETWEEN 3 AND 180),
  summary TEXT NOT NULL CHECK (length(btrim(summary)) BETWEEN 2 AND 4000),
  happened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_crm_interactions_customer_time
  ON crm_interactions(user_id,happened_at DESC,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_crm_interactions_actor_time
  ON crm_interactions(actor_user_id,created_at DESC);

CREATE OR REPLACE FUNCTION reject_crm_interaction_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'CRM interactions are append-only; create a correction interaction instead';
END;
$$;

DROP TRIGGER IF EXISTS trg_crm_interactions_immutable ON crm_interactions;
CREATE TRIGGER trg_crm_interactions_immutable
  BEFORE UPDATE OR DELETE ON crm_interactions
  FOR EACH ROW EXECUTE FUNCTION reject_crm_interaction_mutation();

CREATE TABLE IF NOT EXISTS crm_followups (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  assigned_to UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_by UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  title VARCHAR(180) NOT NULL CHECK (length(btrim(title)) BETWEEN 3 AND 180),
  details TEXT NOT NULL DEFAULT '' CHECK (length(details) <= 2000),
  due_at TIMESTAMPTZ NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'open'
    CHECK (status IN ('open','completed','cancelled')),
  completed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK ((status='completed') = (completed_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_crm_followups_status_due
  ON crm_followups(status,due_at ASC,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_crm_followups_assignee_status_due
  ON crm_followups(assigned_to,status,due_at ASC);
CREATE INDEX IF NOT EXISTS idx_crm_followups_customer_time
  ON crm_followups(user_id,created_at DESC);

COMMIT;
