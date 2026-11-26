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
- Production hardening gates remain: Google OAuth/verified linking, password reset/email verification, IP/distributed rate limiting, CSRF/CORS/security headers, superadmin bootstrap, SMS worker and deployment security review.

## Phase 3 — Account and dashboard API
- Authenticated profile read/update, allow-listed preferences, session listing/revocation, subscription history and server-resolved entitlements.
- Dashboard frontend and PostgreSQL runtime coverage remain separate follow-up work.

## Phase 4 — Subscription/payment lifecycle (PostgreSQL-tested; gateway onboarding blocked)
- Server-priced, phone-verified, idempotent orders and hosted checkout attempt lifecycle.
- Atomic settlement, amount/provider-reference validation, callback replay protection and serialized renewals.
- Expired-order maintenance, audited full-refund queue/worker contract and stale-payment reconciliation queue.
- Live provider integration remains blocked on official merchant docs and credentials; no live payments.

## Phase 5 — Wallet and referrals (PostgreSQL-tested)
- Isolated cash/gift wallet with append-only idempotent ledger and gift-first spending.
- Withdrawal reservation, verified-bank-account ownership gate, admin approval/rejection and audited confirmed payout.
- Server-generated referral codes, verified-phone attachment and one-time gift reward after first paid subscription settlement.
- Extended regression suite: 139 passed, including PostgreSQL 16, concurrency, replay, rejection/cancellation, bank verification and migration re-run.
- Wallet top-ups, bank verification onboarding, automated payouts and advanced abuse controls remain gated on provider/security work.

## Phase 6 — Support (implemented; validation in CI)
- Public published FAQ, authenticated ticket creation/list/detail/replies and strict ticket ownership.
- Staff queue with assignment, status transitions, staff replies and private internal notes.
- Permission checks for tickets.manage and faq.manage are enforced in both API and service layers; staff mutations and FAQ changes are audited.
- User-facing ticket reads never include internal notes. Real-session API regression covers the complete user/staff/FAQ lifecycle, owner isolation and revoked-session denial.

## Phase 7 — Product analytics (backend implemented; frontend/privacy gates remain)
- Opt-in account consent and revocation; allow-listed, idempotent authenticated event ingestion.
- Page views, clicks, navigation, scroll depth, active duration and explicit funnel steps.
- Permission-gated aggregate overview, funnels, weekly retention and audited user-level activity.
- Authenticated self-service event deletion also revokes consent transactionally; append-only consent history is retained.
- Heatmap data is component interaction intensity only; no pointer tracking or keystroke capture.
- Frontend consent/instrumentation, event-retention schedule, data export and legal/privacy review remain.

## Phase 8 — Administrative console (permission-scoped backend implemented; browser UI follow-up)
- Authenticated console API provides overview, user search/detail/status, role management, subscription/payment views, reconciliation, wallet detail/adjustments, withdrawals, referrals, support queue and audit.
- Every service enforces its own permission; the overview and referral administration are superadmin-only. Superadmin role provisioning and modification remain out-of-band.
- Suspending an account revokes its active sessions; self-suspension and superadmin suspension are blocked.
- Wallet adjustments use integer Toman, immutable ledger entries, idempotency keys and audit records. No bank transfer or external payout is initiated by the console.
- Migration 007 adds only console/referral permissions to the isolated SaaS database.
- Browser UI, CSRF/Origin hardening and production bootstrap remain before production enablement.

## Phase 9 — Production readiness
Threat model, security review, automated/load tests, backup/restore, monitoring,
incident response, deployment automation and legal review.

CRM is explicitly deferred.
