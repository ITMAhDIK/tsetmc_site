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

## Phase 5 — Wallet and referrals (foundation started)
Implemented on feat/tloomberg-saas-wallet-referrals-phase5: separate cash/gift
ledger balances, append-only idempotent ledger posting, gift-first spending policy,
cash-only withdrawal reservations, verified-bank-account gating, referral codes,
verified-phone referral attachment, and reward issuance from the trusted paid
subscription settlement path. Top-up checkout, bank onboarding/verification,
admin withdrawal review, payout integration and advanced abuse controls remain
blocked or deferred pending provider contracts and security work. No market-data
database or service changes.

## Phase 6 — Support (API foundation started)
Implemented on feat/tloomberg-saas-support-phase6: public published-FAQ reads,
customer ticket creation/list/detail/replies, staff queue and conversation access,
assignment to active support/admin users, status changes, a separate internal-note
endpoint, role-gated FAQ management and audit records. Attachments, notifications,
staff/customer UI, SLA and advanced anti-abuse controls remain. No market-data
database or service changes.

## Phase 7 — Product analytics (foundation started)
Implemented on feat/tloomberg-saas-product-analytics-phase7: versioned opt-in
consent, authenticated consent-gated event batches, server-derived user/session
identity, event deduplication, privacy-minimized page/click/scroll/active-time
fields, aggregate overview/pages/heatmap/sequential funnels/retention reports,
minimum-cohort suppression and separately permission-gated user-level event
history. Anonymous tracking, frontend SDK/banner, dashboard UI and automatic
retention scheduling remain. No market-data database or service changes.

## Phase 8 — Superadmin console (API foundation started)
Implemented on feat/tloomberg-saas-superadmin-phase8: superadmin-only dashboard
and APIs for user management, support/admin role assignment, plans, subscriptions,
payment reconciliation notes, wallet inspection and audited ledger adjustments,
withdrawal review, referral visibility and audit logs. Superadmin role changes
remain out-of-band; actual bank payout and provider payment overrides are not
allowed. Frontend UI and database/security integration tests remain. No market
data DB or service changes.

## Phase 9 — Production readiness (in progress)
Phase 9 is a release-readiness programme for the complete Phase 1–8 chain.
Phase 9A on feat/tloomberg-saas-production-readiness-phase9 adds a threat model
and release gates, backup/restore runbook and guarded PostgreSQL backup plus
isolated restore-verification scripts, additional authentication security tests,
and automated dependency/SAST checks. These are engineering foundations only:
scripts have not been exercised against a live PostgreSQL instance, and CI scans
must be observed on the pull request. Database/RBAC/provider concurrency tests,
independent penetration review, load/soak tests, encrypted off-host backups,
monitoring/alerting, deployment/rollback automation and jurisdiction-specific
legal review remain mandatory before public production.

CRM is explicitly deferred.
