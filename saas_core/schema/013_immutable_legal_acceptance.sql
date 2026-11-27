-- Legal acceptance is compliance evidence and must remain append-only.
BEGIN;

CREATE OR REPLACE FUNCTION reject_legal_acceptance_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'legal_acceptances are append-only; record a new acceptance for a new version';
END;
$$;

DROP TRIGGER IF EXISTS trg_legal_acceptances_immutable ON legal_acceptances;
CREATE TRIGGER trg_legal_acceptances_immutable
  BEFORE UPDATE OR DELETE ON legal_acceptances
  FOR EACH ROW EXECUTE FUNCTION reject_legal_acceptance_mutation();

COMMIT;
