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
- Permission checks for tickets.manage and faq.manage; staff mutations and FAQ changes are audited.
- User-facing ticket reads never include internal notes.

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
