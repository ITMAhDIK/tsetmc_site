# Tloomberg SaaS Delivery Roadmap

## Phase 1 — Foundation (this branch)
- Define isolated market, SaaS and analytics data boundaries.
- Add PostgreSQL schema and seed subscription plans.
- Define identity, RBAC, payments, wallet, referrals, support, consent and audit.
- Implement and test cash/gift wallet policy.
- Keep existing market API/database untouched.

## Phase 2 — Authentication and authorization
Username/password, phone OTP, Google OAuth, verified identity linking,
revocable sessions, password reset, rate limiting, server-side entitlements,
and one-time out-of-band superadmin bootstrap.

## Phase 3 — Account and dashboard UX
Menu, profile, preferences, sessions, account/subscription history and feature
gates. Finalize the free-tier access matrix before implementation.

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
- [x] Permission-gated user, role, subscription and overview operations.
- [x] Payment reconciliation queue and append-only human review records.
- [x] Wallet/withdrawal operations, referrals, support queues, FAQ and audit.
- [ ] Authenticated HTTP adapter, privileged-session MFA/revocation and browser console UI.

## Phase 9 — Production readiness
- [x] Threat model, security gates and incident/monitoring/backup runbooks.
- [x] SaaS-only healthcheck and verified PostgreSQL backup/restore tooling.
- [x] Bounded GET-only staging load probe with explicit acknowledgement.
- [x] CI validation of migrations 001–004 on PostgreSQL and backup/restore round-trip.
- [ ] Hosting-specific deployment, external monitoring/alerting and encrypted backup retention.
- [ ] Independent security review, privacy/legal approval and production go-live sign-off.

CRM is explicitly deferred.
