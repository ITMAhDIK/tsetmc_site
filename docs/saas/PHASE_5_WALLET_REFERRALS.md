# Phase 5 — Wallet and referrals

## Scope implemented in this foundation
- Wallet balance is derived from immutable, append-only ledger entries; cash and gift buckets remain separate.
- Gift balance is spent before cash and is never withdrawable.
- Withdrawal requests reserve cash while in requested/approved states; cancellation releases the reservation.
- A withdrawal can only target a bank-account record marked verified. Raw bank details are not accepted by these endpoints.
- Referral codes are generated server-side. Self-referrals and referrals from inactive accounts are rejected.
- Referral attachment requires the referred account to have a verified phone.
- Referral qualification/reward is a trusted server-side operation and requires a paid subscription order linked to a subscription. Reward is credited to the inviter's gift bucket and is idempotent.

## API routes
- GET /wallet — cash, gift, total, reserved and withdrawable balances.
- GET /wallet/ledger — user's own ledger history.
- POST /wallet/withdrawals — reserve cash for a verified bank account.
- POST /wallet/withdrawals/{withdrawal_id}/cancel — cancel a still-requested withdrawal.
- GET /wallet/referrals — own referral code and aggregate counts.
- POST /wallet/referrals/attach — attach a referral code to the authenticated account.

## Explicitly not enabled yet
- Top-up checkout and settlement are not wired to a live provider. They require an official merchant integration contract and sandbox credentials; no provider protocol is guessed.
- Bank-account onboarding/verification provider is not implemented. Until an approved verification adapter exists, no account can be marked verified through the public API, so withdrawals remain fail-closed.
- Admin approval/rejection and payout execution endpoints are not exposed. The internal settlement helper only records a debit after an approved payout is confirmed.
- Referral reward amount is currently the schema's 100,000 Toman default; it must become a versioned business setting before launch.
- Device/IP/household abuse scoring and distributed rate limits remain for the security-hardening phase.

## Security and data boundaries
- All persistence uses the dedicated SaaS PostgreSQL connection.
- Ledger rows are never updated or deleted; corrections require compensating entries.
- Users can only inspect their own wallet, ledger, withdrawal and referral records.
- No market-data database, collector, API or schema is accessed or modified.

## Validation status
Pure wallet-policy tests are included. PostgreSQL integration, concurrency, bank verification, top-up sandbox and abuse-control tests have not been run. Keep this API private and this PR Draft.


## Microservices extraction status — Phase 2

Wallet, withdrawal and referral persistence/policies now belong to the independent
Commerce service and its own PostgreSQL database. The descriptions above are the
original SaaS foundation; see `docs/commerce/PHASE_2_COMMERCE_SERVICE.md` for the
current service boundary and migration requirements. Bank verification, payout
execution and live wallet top-ups remain deliberately unavailable.
