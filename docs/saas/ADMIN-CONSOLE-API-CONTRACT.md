# Phase 8 — Console API contract

The Phase 8 domain service is not a public HTTP server. A future authenticated
SaaS adapter should expose staff-only routes such as:

- `GET /api/v1/admin/overview`
- `GET /api/v1/admin/users`, `GET /api/v1/admin/users/{id}`
- `POST /api/v1/admin/users/{id}/status`
- `POST /api/v1/admin/users/{id}/roles`, `DELETE /api/v1/admin/users/{id}/roles/{role}`
- `GET /api/v1/admin/subscriptions`
- `GET /api/v1/admin/payments/reconciliation`,
  `POST /api/v1/admin/payments/{id}/review`
- `GET /api/v1/admin/users/{id}/wallet`,
  `POST /api/v1/admin/users/{id}/wallet/adjustments`
- `GET /api/v1/admin/withdrawals`,
  `POST /api/v1/admin/withdrawals/{id}/review`
- `GET /api/v1/admin/referrals`, `GET /api/v1/admin/tickets`
- `PATCH /api/v1/admin/tickets/{id}`,
  `POST /api/v1/admin/tickets/{id}/internal-notes`
- `PUT /api/v1/admin/faq/{slug}`, `GET /api/v1/admin/audit-logs`

The adapter must derive the actor ID from a validated secure session, call the
service with that trusted ID and map its 400/403/404/409 errors consistently.
The browser must not submit role claims. Privileged sessions should require
MFA and be revoked after role or user-status changes. The routes above are a
contract only, not live endpoints.
