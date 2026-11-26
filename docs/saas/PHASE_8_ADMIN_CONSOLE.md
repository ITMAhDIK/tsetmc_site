# Phase 8 — Administrative console

## Scope implemented
The authenticated administrative API is mounted under `/admin/console` and shares the Phase 2 session mechanism. It provides:

- Overview counters across users, subscriptions, pending payments, reconciliation, withdrawals, referrals, support and analytics events.
- User search and safe account details, account suspension/reactivation, and role grant/revoke for ordinary administrative roles.
- Subscription and payment-order listings; payment reconciliation case listing and audited resolution.
- Per-user wallet balances/ledger and audited idempotent cash/gift adjustments.
- Withdrawal queue, referral attribution/reward views, support queue and audit-log search.

## Authorization and safety
- Each service function independently enforces the required permission; API route checks are not the only boundary.
- The overview and referral administration are superadmin-only. Existing role permissions continue to govern each module.
- Superadmin cannot be assigned or modified through the console; bootstrap and superadmin changes stay out of band.
- Self-suspension is rejected. Suspending a user revokes all their active sessions.
- Wallet adjustments use integer Toman and append-only ledger entries with a reason and idempotency key. Debits cannot exceed the selected bucket balance.
- Payment reconciliation resolution is audited. The console does not simulate payment-provider verification or initiate bank transfers.
- Sensitive hashes and session tokens are never returned by user-detail queries.

## API surface
- `GET /admin/console/overview`
- `GET /admin/console/users`, `GET /admin/console/users/{id}`
- `PATCH /admin/console/users/{id}/status`
- `GET /admin/console/roles`, `PUT|DELETE /admin/console/users/{id}/roles/{role}`
- `GET /admin/console/subscriptions`, `GET /admin/console/payments`
- `GET /admin/console/payments/reconciliation`, `POST /admin/console/payments/reconciliation/{id}/resolve`
- `GET /admin/console/wallets/{user_id}`, `POST /admin/console/wallets/{user_id}/adjust`
- `GET /admin/console/withdrawals`, `GET /admin/console/referrals`
- `GET /admin/console/support`, `GET /admin/console/audit`

## Validation and remaining work
PostgreSQL integration tests cover role separation, user suspension/session revocation, sensitive-field exclusion, role-provisioning restrictions, cross-domain views, reconciliation and wallet adjustment idempotency/sign constraints.

The browser console is served from the authenticated SaaS API at `GET /admin/console/ui` (same origin only). Cookie-authenticated mutations require a matching Origin; configure `SAAS_PUBLIC_ORIGIN` behind a reverse proxy. The UI uses same-origin API calls and never receives the HttpOnly session token. No live payment, bank transfer, payout or production deployment is performed by this phase.
