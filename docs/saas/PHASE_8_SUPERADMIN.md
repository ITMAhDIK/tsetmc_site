# Phase 8 — Superadmin console

## Implemented foundation
- Superadmin-only console guard layered on the existing capability permissions.
- Dashboard summary counts for users, active subscriptions, pending payments, recent paid orders, pending withdrawals, open support tickets, pending referrals and recent analytics events.
- Searchable/paginated user list and user detail with roles, recent sessions, subscriptions and analytics-consent state.
- User suspension/reactivation; suspending a user revokes all of their active sessions. Deleted/pending accounts cannot be reactivated, superadmin accounts cannot be changed, and the last active superadmin cannot be suspended.
- Role listing and assignment/removal for support/admin only. Superadmin role bootstrap/removal is intentionally out-of-band; a logged-in operator cannot self-modify roles.
- Plan listing and audited updates to title, duration, price and active state. Price changes apply only to new payment orders; existing order amounts are not rewritten.
- Subscription listing and audited cancellation; the console does not manually activate subscriptions.
- Payment order listing and append-only reconciliation notes. Reconciliation notes document an investigation; they cannot mark an order paid or override provider verification.
- Per-user wallet inspection and idempotent, audited cash/gift adjustments through append-only ledger entries. Idempotency keys are bound to the exact target, bucket, amount and reason; amounts are strict signed 64-bit integers and an adjustment cannot make either bucket negative.
- Withdrawal queue and approve/reject workflow. Actual bank payout and marking a withdrawal paid are deliberately not implemented without a provider integration.
- Referral listing and visibility into rewards.
- Audit-log filtering and read access.
- Existing Phase 6 support-management routes and Phase 7 analytics reports remain available under their own permissions.

## API routes
- GET /admin/dashboard
- GET /admin/users
- GET /admin/users/{user_id}
- PATCH /admin/users/{user_id}/status
- GET /admin/roles
- PUT /admin/users/{user_id}/roles/{role_code}
- GET /admin/plans
- PATCH /admin/plans/{plan_id}
- GET /admin/subscriptions
- POST /admin/subscriptions/{subscription_id}/cancel
- GET /admin/payments/orders
- POST /admin/payments/orders/{order_id}/reconciliation-notes
- GET /admin/payments/orders/{order_id}/reconciliation-notes
- GET /admin/users/{user_id}/wallet
- POST /admin/users/{user_id}/wallet/adjustments
- GET /admin/withdrawals
- POST /admin/withdrawals/{withdrawal_id}/review
- GET /admin/referrals
- GET /admin/audit-logs

## Security and data boundaries
- Every console endpoint requires both superadmin.console.access and the specific action permission.
- No endpoint can grant/revoke the superadmin role; initial bootstrap and break-glass recovery stay out-of-band.
- The last active superadmin cannot be suspended, and actors cannot change their own admin/support role.
- Suspending a user revokes their sessions.
- Wallet writes are append-only, idempotent and audited; negative bucket balances are rejected.
- Reconciliation notes are append-only. Payment state is never manually changed by an operator.
- Withdrawal decisions are separate from real-world payout confirmation; no fake "paid" endpoint exists.
- Bank-account vault tokens are not returned by the admin withdrawal listing.
- All persistence uses the dedicated SaaS PostgreSQL database. The market-data DB, collector, market API and GitHub Pages market frontend are not accessed or modified.

## Not included
- React/HTML admin frontend, charts and role-specific UI.
- Provider-backed refund reconciliation and actual bank payouts.
- Outbound notifications, export jobs, bulk actions and advanced search indexing.
- Two-person approval workflow for large wallet adjustments.
- Independent PostgreSQL 16 integration tests now exercise authenticated API access, RBAC separation, account/session changes, role protections, plan/subscription updates, append-only reconciliation notes, wallet idempotency and withdrawal review. Concurrent stress testing and independent security review remain outstanding.

## Validation status
Pure policy/input tests and PostgreSQL/API integration tests are included. GitHub Actions now provisions PostgreSQL 16 and runs the full suite with migrations 001–006 in an isolated test schema. The new CI result must be green before this phase is considered validated. Keep this PR Draft. Do not expose the console publicly or apply the migration until database integration and security tests are complete.
