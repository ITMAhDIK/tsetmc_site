# Phase 8 — Staff console API contract

The authenticated SaaS adapter is implemented in
`saas_core.http.admin_api.SaasAdminAPI`. It is a WSGI application and must be
mounted only behind the approved HTTPS SaaS host with an explicit
`allowed_origins` configuration.

## Authentication and request rules

- The actor is derived exclusively from the server-side session cookie; actor
  IDs, roles, and permissions supplied by the browser are ignored.
- The session must be active, unexpired, and have a recent server-recorded MFA
  verification.
- Every state-changing request requires an allow-listed Origin and a
  session-bound CSRF token in both the CSRF cookie and `X-CSRF-Token` header.
- Responses use a consistent JSON envelope and map domain errors to HTTP
  400/403/404/409 responses. Unexpected failures return a generic 500 response
  without SQL or secret details.

## Implemented routes

- `GET /api/v1/admin/me`
- `GET /api/v1/admin/overview`
- `GET /api/v1/admin/roles`
- `GET /api/v1/admin/users`, `GET /api/v1/admin/users/{id}`
- `PATCH /api/v1/admin/users/{id}/status`
- `POST /api/v1/admin/users/{id}/roles`,
  `DELETE /api/v1/admin/users/{id}/roles/{role}`
- `GET /api/v1/admin/subscriptions`,
  `POST /api/v1/admin/subscriptions/{id}/cancel`
- `GET /api/v1/admin/payments/reconciliation`,
  `POST /api/v1/admin/payments/{id}/review`
- `GET /api/v1/admin/users/{id}/wallet`,
  `POST /api/v1/admin/users/{id}/wallet/adjustments`
- `GET /api/v1/admin/withdrawals`,
  `POST /api/v1/admin/withdrawals/{id}/review`
- `GET /api/v1/admin/referrals`
- `GET /api/v1/admin/tickets`, `GET /api/v1/admin/tickets/{id}`
- `PATCH /api/v1/admin/tickets/{id}`,
  `POST /api/v1/admin/tickets/{id}/internal-notes`,
  `POST /api/v1/admin/tickets/{id}/replies`
- `GET /api/v1/admin/faq`, `PUT /api/v1/admin/faq/{slug}`,
  `DELETE /api/v1/admin/faq/{slug}`
- `GET /api/v1/admin/analytics/summary`,
  `GET /api/v1/admin/analytics/funnels`,
  `GET /api/v1/admin/analytics/cohorts`,
  `GET /api/v1/admin/analytics/components`
- `GET /api/v1/admin/audit-logs`

## Browser console

The Persian RTL console is `docs/saas/admin.html`. Its
`meta[name="tloomberg-admin-api"]` value is intentionally blank in the
repository, so the browser cannot send requests until an approved deployment
provides the API origin. The page uses credentialed requests and reads the
session-bound CSRF cookie for mutations.

## Verification

The standard Python CI workflow runs Phase 8 PostgreSQL integration tests on a
disposable PostgreSQL 18 instance after applying migrations 001–005. Coverage
includes transactional payment review and audit, last-superadmin protection,
and real session/MFA/role authorization through the WSGI adapter.

No production deployment or external payment/bank operation is implied by
passing these tests.
