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
- Initial combined branch CI: static Pages validation passed; 83 tests passed.
- Not production-ready: Google OAuth/verified linking, password reset/email verification, IP/distributed rate limiting, CSRF/CORS/security headers, superadmin bootstrap, SMS delivery worker, PostgreSQL integration tests and deployment security review remain.

## Phase 3 — Account and dashboard API (implemented; integration validation pending)
- Authenticated profile read/update (display name only).
- Allow-listed theme, calendar, currency and locale preferences.
- Active session listing, single-other-session revocation and revoke-all-others; current session is protected.
- Subscription history read model and server-resolved feature entitlements.
- No market-data schema, database, collector or API changes.
- Dashboard frontend and PostgreSQL integration tests remain.

## Phase 4 — Subscriptions and payments
SnapPay/DigiPay adapters, merchant onboarding, sandbox tests, verified phone,
idempotent order lifecycle, verification/settlement, refunds and reconciliation.

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
