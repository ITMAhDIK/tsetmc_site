# Tloomberg SaaS Delivery Roadmap

## Phase 1 — Foundation
- Define isolated market, SaaS and analytics data boundaries.
- Add PostgreSQL schema and seed subscription plans.
- Define identity, RBAC, payments, wallet, referrals, support, consent and audit.
- Implement wallet policy.
- Keep existing market API/database untouched.

## Phase 2 — Authentication and authorization (in progress)
Implemented on feat/tloomberg-saas-auth-phase2: username/password with Argon2id,
phone OTP challenge persistence, encrypted SMS outbox, per-phone OTP request
throttling, revocable opaque sessions, and server-side role/entitlement policy.
Remaining before production: Google OAuth, verified identity linking, password
reset, IP/distributed rate limiting, CSRF/CORS hardening, superadmin bootstrap,
SMS worker, PostgreSQL integration tests and deployment security review.

## Phase 3 — Account and dashboard UX (API foundation started)
Implemented on feat/tloomberg-saas-account-phase3: profile read/update, allow-listed
preferences, active session listing/revocation, subscription history and
server-resolved feature access. Dashboard frontend and PostgreSQL integration
tests remain. The current session cannot be revoked through the other-session
route. No market-data database or service changes.

## Phase 4 — Subscriptions and payments (lifecycle foundation started)
Implemented on feat/tloomberg-saas-payments-phase4: server-priced idempotent
subscription orders, verified-phone purchase gate, provider-neutral checkout and
verification contracts, payment attempt tracking, atomic/idempotent settlement,
subscription renewal extension, and refund-request schema. SnapPay/DigiPay live
adapters remain disabled pending official merchant integration packs and sandbox
credentials. Refund execution, reconciliation, PostgreSQL concurrency tests and
provider end-to-end tests remain.

## Phase 5 — Wallet and referrals
Ledger, top-ups, gift/cash balance, withdrawal reservations, bank verification,
referral qualification, abuse controls and SMS outbox.

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
