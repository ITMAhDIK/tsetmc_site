# Phase 8 — Superadmin and staff console

Branch: `refactor/architecture-stabilization-phase0`

## Delivered

The Phase 8 boundary is implemented as a SaaS domain service, an authenticated
WSGI adapter, and a staff-console browser page:

- `saas_core.domain.admin_console.AdminConsoleService` implements the
  permission-gated administration operations against the dedicated SaaS
  PostgreSQL connection.
- `saas_core.http.admin_api.SaasAdminAPI` exposes the versioned staff API.
  It derives the actor from a hashed server-side session; browser payloads
  cannot select the actor, role, or permission.
- `docs/saas/admin.html` provides the Persian RTL staff-console interface.
  The API origin is intentionally empty by default and must be configured only
  for an approved same-site HTTPS deployment.
- The console covers overview, users and roles, subscriptions, payment review,
  wallet ledger review and adjustments, withdrawals, referrals, support
  tickets and private notes, FAQ management, analytics reports, and audit logs.

### Domain invariants

- User suspension revokes active sessions. The last active superadmin cannot
  be suspended or have the superadmin role removed.
- Administrative role elevation is checked against the active server-side
  superadmin identity.
- Payment review creates append-only reconciliation evidence. It does not
  settle a payment or activate a subscription; only verified provider
  settlement may do that.
- Wallet changes are append-only, idempotent ledger entries. Cash debits
  preserve outstanding withdrawal reservations.
- A withdrawal may be recorded as paid only with a non-empty payout reference;
  the operation records the externally confirmed payout and its ledger debit.
  It does not call a bank or payment provider.
- Internal support notes are stored as internal messages, separate from public
  replies.
- Administrative mutations and their audit records share one database
  transaction. Audit and payment-review evidence is protected against update
  and delete by migration 005.

## Authentication and authorization

- The adapter accepts only a valid, active, unexpired server-side session.
- Staff requests require a recent server-recorded MFA verification.
- Permission checks are resolved from the current user, role, and permission
  rows in the SaaS database on the server.
- State-changing requests require an allowed Origin and a session-bound
  double-submit CSRF token.
- The service receives only the SaaS connection factory; market-data databases
  and Sidecar connections are not dependencies of this domain.

## Integration verification

The standard Python CI workflow provisions a disposable PostgreSQL 18 service,
applies SaaS migrations 001–005, and runs the unit and integration suites.
Phase 8 PostgreSQL integration tests exercise:

- Payment review through the real domain service, verifying the review row and
  audit row commit while the payment order remains pending.
- The last-active-superadmin guard against real role and user rows, verifying
  both attempted suspension and superadmin-role revocation are rejected and
  leave the database unchanged.
- The WSGI admin API with real database-backed sessions and role permissions,
  verifying an MFA-authenticated superadmin can read the overview while a
  normal user receives a permission-denied response.

The latest full PostgreSQL-backed Python CI run completed with **250 passed
in 4.91 seconds**. The separate production-readiness Python job completed with
247 passed and the three database-dependent integration tests skipped because
that job does not configure the integration-test database.

These are disposable CI databases. They are not production database tests.

## Deployment boundary

Phase 8 code and integration tests are present, but this does not mean the
console is deployed or publicly reachable. The API origin remains unconfigured
by default. No production database, production Sidecar, payment provider, bank
payout integration, or live service was modified or exercised. Do not expose
the console until the separate production and security release gates have been
reviewed and approved.
