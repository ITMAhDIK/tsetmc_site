# Tloomberg SaaS Delivery Roadmap

## Phase 1 — Foundation
- Isolated market, SaaS and analytics data boundaries.
- SaaS PostgreSQL schema and core identity, RBAC, payments, wallet, referrals, support, consent and audit structures.
- Cash/gift wallet policy and tests.
- Existing market API/database remain separate.

## Phase 2 — Authentication and authorization (foundation integrated; hardening remains)
- Argon2id password registration/login, opaque revocable sessions and secure cookies.
- Phone OTP challenge persistence, encrypted SMS outbox and per-phone request throttling.
- Server-side role, permission and subscription-entitlement policy.
- Initial combined branch CI: 83 tests passed.
- Not production-ready: Google OAuth/verified linking, password reset/email verification, IP/distributed rate limiting, CSRF/CORS/security headers, superadmin bootstrap, SMS delivery worker, PostgreSQL integration tests and deployment security review remain.

## Phase 3 — Account and dashboard API (implemented; integration validation pending)
- Authenticated profile read/update (display name only).
- Allow-listed theme, calendar, currency and locale preferences.
- Active session listing, single-other-session revocation and revoke-all-others; current session is protected.
- Subscription history read model and server-resolved feature entitlements.
- No market-data schema, database, collector or API changes.
- Dashboard frontend and PostgreSQL integration tests remain.

## Phase 4 — Subscription/payment lifecycle (PostgreSQL-tested; gateway onboarding blocked)
- Server-priced, phone-verified, idempotent orders and hosted checkout attempt lifecycle.
- Atomic settlement, amount/provider-reference validation, callback replay protection and serialized renewals.
- Expired-order maintenance, audited full-refund queue/worker contract and stale-payment reconciliation queue.
- Disposable PostgreSQL 16 integration and concurrency tests run in CI.
- SnapPay/DigiPay live adapters, provider callback authenticity and sandbox E2E remain blocked on official merchant docs and credentials; no live payments.

## Phase 5 — Wallet and referrals (integrated; external bank/provider gates remain)
- Isolated SaaS wallet with append-only idempotent ledger, cash/gift buckets and gift-first spending.
- Cash-only withdrawal reservations, verified bank-account gate, admin approval/rejection and audited confirmed payout settlement.
- Server-generated referral codes and verified-phone referral attachment.
- First verified paid subscription settlement issues one idempotent gift reward using a versioned reward policy.
- PostgreSQL 16 integration/concurrency tests and migration re-run coverage.
- Wallet top-ups, bank verification onboarding, automated payouts and advanced abuse controls remain blocked/deferred pending provider contracts and security work. No market-data database or service changes.

## Phase 6 — Support
FAQ, ticket creation/replies, assignment, status and private internal notes.

## Phase 7 — Product analytics
Consent, click/navigation/scroll/active-time events, heatmaps, funnels, retention
and permission-gated user-level analysis.

## Phase 8 — Superadmin console
Users, roles, subscriptions, payment reconciliation, wallets, withdrawals,
referrals, support queues, analytics and audit.

## Phase 9 — Production readiness
Threat model, security review, automated/load tests, backup/restore, monitoring,
incident response, deployment automation and legal review.

CRM is explicitly deferred.
