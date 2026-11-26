# Phase 8 — Superadmin and staff console domain

Branch: `refactor/architecture-stabilization-phase0`

## Delivered

`saas_core.domain.admin_console.AdminConsoleService` provides permission-gated
operations over the dedicated SaaS PostgreSQL database:

- Overview counts for users, subscriptions, payment review, withdrawals,
  support tickets and referrals.
- User search, safe profile details, activation/suspension and role assignment
  or revocation. Accounts are never physically deleted by console actions.
- Last-active-superadmin protection and server-side superadmin checks for
  administrative role elevation.
- Subscription lists and payment reconciliation queue.
- Append-only payment review records. A review does **not** mark a payment paid
  or activate a subscription; only the verified provider settlement workflow
  may do that.
- Cash/gift wallet review and idempotent compensating ledger adjustments.
  Debits respect pending/approved cash withdrawal reservations.
- Withdrawal workflow with explicit allowed transitions; recording a payout
  as paid requires a non-empty payout reference.
- Referral activity, ticket queues, active support assignment, internal-only
  notes, FAQ editing and filtered audit-log reads.
- Every state-changing management operation records an audit entry in the same
  database transaction.

Migration 004 adds least-privilege permissions, a payment-review table and a
payout reference field. Apply it after migrations 001–003.

## Security and deployment boundary

The service requires an authoritative permission checker derived from current
SaaS roles. It does not accept roles or permissions from request payloads and
does not receive the market database. Password hashes, OTP hashes, session
tokens and bank account tokens are not returned by list/detail responses.

This is a tested domain/application service, not yet a deployed HTTP API or
finished browser console UI. Route adapters, MFA policy for privileged staff,
session revocation on role change and deployment remain part of production
readiness. Do not expose it publicly until those controls are wired.
