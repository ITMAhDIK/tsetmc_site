-- Phase 5: wallet and referral integrity extensions.
-- Applies only to the dedicated SaaS PostgreSQL database.
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
 UNIQUE(user_id, account_fingerprint),
 UNIQUE(id,user_id)
);
ALTER TABLE withdrawal_requests
 ADD COLUMN IF NOT EXISTS bank_account_id UUID;
ALTER TABLE withdrawal_requests
 ADD CONSTRAINT withdrawal_bank_account_owner_fk
 FOREIGN KEY (bank_account_id,user_id)
 REFERENCES wallet_bank_accounts(id,user_id) ON DELETE RESTRICT;
CREATE INDEX IF NOT EXISTS idx_withdrawals_user_status
 ON withdrawal_requests(user_id,status,requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_referrals_inviter_status
 ON referrals(inviter_user_id,status,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_referral_rewards_user_time
 ON referral_rewards(user_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_wallet_ledger_reference
 ON wallet_ledger_entries(reference_type,reference_id);
-- A referral reward is a single gift-credit ledger entry, never cash.
CREATE UNIQUE INDEX IF NOT EXISTS uq_referral_reward_ledger_entry
 ON referral_rewards(ledger_entry_id) WHERE ledger_entry_id IS NOT NULL;
