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

## Phase 9 — Production readiness (engineering foundation implemented; release gates open)
Phase 9A–D on feat/tloomberg-saas-production-readiness-phase9 now includes:
- Threat model, trust boundaries, explicit release gates and NOT READY status.
- Dependency vulnerability audit (pip-audit), high-severity/high-confidence Bandit, Dependabot weekly checks for Python, GitHub Actions and Docker images, and pinned Actions commit SHAs. Latest confirmed combined-branch run: Python suite 187 passed in 9.28s; dependency audit, security analysis, and PostgreSQL backup/restore drill passed.
- Production startup configuration checks, explicit host/CORS allow-lists, cookie CSRF origin checks, security headers, readiness endpoint, structured redacted request logs and bounded PostgreSQL connection pool.
- Argon2id dummy verification for unknown user logins, serialized concurrent OTP rate limits, exact payment-event and admin-wallet idempotency, append-only wallet/reconciliation/admin audit tables.
- Checksummed migration runner with advisory locking, per-migration transactions, drift detection, exact DB identity check, explicit rejection of unknown/non-empty schema without migration ledger, and bounded grants to a pre-created runtime role.
- PostgreSQL 16 CI integration suite (187 Python tests on the verified combined analytics/Phases 8–9 head) for authentication, RBAC and object isolation, standard-user denial across current staff/admin/analytics-admin GET routes, support internal-note confidentiality, consent analytics, payment settlement/replay, gateway fail-closed behavior, wallet concurrency/idempotency, OTP concurrency and immutable records.
- Age-encrypted custom-format PostgreSQL backup with SHA-256 and isolated decryption/restore. CI runs an ephemeral PostgreSQL 16 backup → encrypted archive → decryption → restore rehearsal using a separately provisioned read-only backup role; it asserts that role cannot mutate tables and verifies remote backup/restore TLS fail-closed behavior.
- Non-root API Docker image and hardened production Compose template with one-shot migration prerequisite, read-only filesystem, dropped capabilities, bounded process count, Caddy automatic HTTPS, public metadata-minimal readiness route, fixed proxy trust boundary, and external/unpublished PostgreSQL.
- A bounded, read-only HTTPS staging probe for /health and /ready with public-DNS checks and explicit non-production operator approval.

**Not production-complete.** PR #17 remains Draft and unmerged. Phases 1–8 remain separate unmerged Draft dependencies. Existing production DB baseline adoption, complete route-level RBAC/object-isolation matrix, official payment/SMS/bank sandbox tests, edge/IP/device/distributed rate limiting and WAF, independent penetration review, encrypted off-host backup retention/RPO/RTO and a real disaster-recovery drill, production monitoring/on-call and incident exercise, executed staging load/soak/failure tests, actual host/DNS/TLS deployment and rollback, and jurisdiction-specific legal/privacy review remain required before public production. No migration has been applied to a live/market DB; no production deployment or live-host load test was attempted.
CRM is explicitly deferred.
