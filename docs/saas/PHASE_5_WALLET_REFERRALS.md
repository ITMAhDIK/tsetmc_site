# Phase 5 — Wallet and referrals

## Implemented
- Cash and gift balances are computed from immutable append-only ledger rows; all values are integer Toman.
- Ledger posting is idempotent, wallet-row serialized and rejects debits that would make a bucket negative.
- Wallet spending posts one or two compensating-safe debit rows atomically, consumes gift first, then only unreserved cash.
- Withdrawal requests are idempotent and reserve cash. Requests require an owner-matched verified bank-account record; cancellation releases a requested reservation.
- Admin withdrawal review requires `withdrawals.review`; approval retains the reservation, rejection releases it, and confirmed payout settlement posts a cash debit plus audit record and unique payout reference.
- Referral codes are server generated; self-referrals, inactive inviters, unverified phones and duplicate attachment are rejected.
- First verified paid subscription settlement automatically qualifies the referral and posts exactly one gift reward in the same database transaction.
- Referral reward amount is read from a versioned active database policy seeded at 100,000 Toman.
- All wallet/referral writes remain in the isolated SaaS PostgreSQL database.

## API
- GET /wallet
- GET /wallet/ledger
- POST /wallet/withdrawals
- POST /wallet/withdrawals/{withdrawal_id}/cancel
- GET /wallet/referrals
- POST /wallet/referrals/attach
- GET /wallet/admin/withdrawals
- POST /wallet/admin/withdrawals/{withdrawal_id}/review
- POST /wallet/admin/withdrawals/{withdrawal_id}/settle

## External integration gates
- Wallet top-up checkout and settlement are not exposed until a supported provider's official merchant integration pack and sandbox credentials are available.
- Bank-account onboarding and identity/account verification are not connected. No public API can mark an account verified; withdrawals stay fail-closed until a trusted bank verification workflow populates the verified bank-account row.
- The settlement endpoint records a payout only after an operator confirms an external bank transfer and supplies its reference; no automated payout integration is claimed.
- Distributed device/IP/household abuse scoring and SMS reward notifications remain deferred to security/notification work.

## Migration and validation
- `003_wallet_referrals.sql` is re-runnable and adds a unique payout reference, verified bank-account ownership constraint and versioned reward policy.
- CI applies migrations 001–003 to disposable PostgreSQL 16 and reruns migrations 002–003 to test idempotency.
- Tests cover gift-first spending, ledger immutability, idempotent posting, cash reservation, concurrent withdrawal oversubscription, admin approval/payout audit and referral reward on first paid settlement.
- No market-data database, collector, schema, API or GitHub Pages runtime changes.
