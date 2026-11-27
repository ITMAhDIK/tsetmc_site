# Notification Service — Phase 11

## Scope and ownership

The Notification Service is an independently deployable FastAPI API and delivery worker.
It owns notification inbox records, delivery preferences, idempotency keys, delivery
attempts, retry/dead-letter state and notification audit events in its own PostgreSQL
database. It does not own user credentials or contact details.

Identity remains the source of truth for sessions and verified contact channels.
The Notification API introspects user sessions using the regular private Identity
service credential. The worker retrieves only verified phone/email destinations from
`GET /internal/notification-contact/{user_id}`, protected by the separate
`NOTIFICATION_IDENTITY_SERVICE_TOKEN` credential. The endpoint never returns
unverified contact destinations or account contacts for inactive users.

## Delivery model

Notification creation is asynchronous. Internal services call
`POST /internal/notifications` with `X-Notification-Token`; the route is hidden
from OpenAPI and blocked by the public Caddy edge. Requests have a bounded recipient
list, allowlisted template key, JSON payload, explicit channels and mandatory
idempotency key. Reusing a key with a different request returns HTTP 409.

A database-backed worker claims due work with PostgreSQL
`FOR UPDATE SKIP LOCKED` and a five-minute lease. It retries transient failures
with bounded exponential backoff and moves exhausted or permanent failures to
`dead`. This is an at-least-once delivery design: a provider can accept a message
immediately before a worker crashes, so providers with idempotency support should
receive a stable delivery ID in a future adapter enhancement. Do not promise exactly
once for external SMS/email delivery.

Supported channels:
- **In-app:** durable inbox, read receipts and per-user opt-out.
- **Email:** SMTP with STARTTLS and verified Identity email only.
- **SMS:** HTTPS bearer-token webhook adapter and verified Identity phone only.

The initial allowlisted templates are welcome, security alert, payment receipt,
subscription expiry and product update. User-controlled template HTML is not accepted;
template variables are allowlisted and HTML-escaped. Marketing delivery is opt-in
and no marketing template is enabled by default.

## API

- `GET /notifications` — authenticated user's own inbox.
- `PATCH /notifications/{id}/read` — mark own visible inbox item read.
- `GET /notifications/preferences` and `PUT /notifications/preferences`.
- Private `POST /internal/notifications` — trusted service enqueue.

The public API is routed at `/api/v1/notifications/*`; the service internally
serves `/notifications/*`. Internal enqueue is reachable only on the backend network.

## Deployment and migration

- Dedicated database: `tloomberg_notifications`.
- Dedicated migration runner with checksum verification and explicit expected DB name.
- Low-privilege runtime role, separate API and worker containers, non-root UID 10010.
- Production containers are read-only, have no Linux capabilities, and run on the
  private backend network.
- Configure SMTP and SMS provider credentials in the protected deployment secret store.
  The repository example file contains placeholders only.

## Verification

The Notification workflow applies/checks the isolated PostgreSQL schema, tests
idempotency, channel opt-outs, queue leases, in-app delivery, private route contracts,
Identity contact boundaries, provider transport requirements and local/production
Compose validity. The repository security workflow also scans Notification code,
audits dependencies, builds the non-root image and validates the production edge route.

## Cutover notes

The existing Identity OTP outbox/SMS worker is intentionally left in place during this
phase. OTP delivery is security-critical and must be migrated with a separate
transactional-outbox bridge and a rehearsed rollback plan; this phase does not silently
switch login/verification OTP delivery. Likewise, business-event publishers (payment,
subscription, CRM and account lifecycle) must be enabled with their own service tokens
and stable event IDs before those events are sent to customers. The Notification API
and worker are ready to receive those events, but no production provider or real
customer message is activated by CI.
