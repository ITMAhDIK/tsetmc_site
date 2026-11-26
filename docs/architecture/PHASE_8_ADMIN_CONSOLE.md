# Phase 8 — Superadmin and staff console

Branch: `refactor/architecture-stabilization-phase0`

## Delivered

Phase 8 is implemented as a SaaS application/domain service, FastAPI router
and Persian right-to-left browser console. It uses only the dedicated SaaS
PostgreSQL connection and never opens Market data or Sidecar connections.

- `saas_core/admin/service.py`: dashboard, users, status/roles, plans,
  subscriptions, payment reconciliation, wallet review/adjustments,
  withdrawals, referrals and audit-log operations.
- `saas_core/admin/api.py`: permission-gated admin routes with the
  server-side MFA dependency applied to the complete router.
- `docs/saas/admin.html`: staff console packaged into the same-origin Edge
  image. API requests remain on the first-party HTTPS origin.
- Support and product analytics retain separate SaaS routers and permission
  boundaries; the console consumes their staff/report endpoints.

## Domain operations

The router exposes dashboard; user list/detail/status and roles; plans and
subscriptions; payment orders and reconciliation notes; user wallet and
adjustments; withdrawal review; referrals; and audit logs.

Invariant enforcement:

- Actor, role and permission are derived server-side from the session and
  database; the browser cannot select them.
- Every admin route requires an enrolled privileged account and a recent,
  session-bound TOTP verification.
- TOTP seeds use an independent Fernet key. Replays are rejected and session
  verification expires after 12 hours.
- The last active superadmin cannot be suspended or have the superadmin role
  revoked.
- Mutations and their audit records share a transaction. Audit, financial
  ledger and reconciliation evidence are append-only.
- Payment review writes reconciliation evidence only; it never settles a
  provider payment or activates a subscription.
- Wallet corrections are compensating ledger entries. Withdrawal review does
  not execute a bank payout.
- Internal support notes remain invisible to customer ticket views.
- No Market database or Sidecar is a dependency of the SaaS admin service.

## Auth and legal gates

The login page uses the six-digit FastAPI phone-OTP contract; demo login is
disabled. Password and phone registration require exact server-configured
terms and privacy versions. The versions are stored on the signup OTP
challenge, checked again at verification and inserted from the challenge.
Legal acceptance rows are append-only (migration 013). The checked-in legal
pages are placeholders; registration remains disabled until the owner/legal
reviewer publishes final text and configures matching versions.

## PostgreSQL integration tests

Migrations 001–013 are applied to disposable PostgreSQL databases. Tests cover
real API sessions/roles, last-superadmin protection, payment review that leaves
orders pending, MFA enrollment/replay/expiry, login OTP versus explicit signup,
signup version relabelling rejection, legal append-only enforcement, and
password-signup transaction rollback when acceptance persistence fails.
Browser page tests also validate inline script syntax and actual route families.

## Latest verified CI

Commit: `fb0a15fa6c627fa3fd075ccc56e31efdf50f4c34`

- **373 passed, 0 skipped** in the full Python/PostgreSQL suite.
- Dependency audit, high-severity Bandit, image/Compose/Caddy validation and
  PostgreSQL backup/restore drill: all successful.

## External go-live gates

The code and deployment templates are present; no host has been provisioned.
Before public launch: configure DNS/TLS/firewall, immutable image digests,
PostgreSQL and its roles, protected secrets, the SMS provider, final legal
documents/versions, TOTP recovery, off-host bucket retention/object lock,
Alertmanager delivery/on-call, host monitoring and independent security review.
