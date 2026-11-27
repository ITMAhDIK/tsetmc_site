# Identity Service — Phase 1

Identity is a separately deployable FastAPI service with its own PostgreSQL database. It is the sole authority for credentials, password hashes, OTP challenges, opaque sessions, TOTP MFA, roles, permissions and immutable legal-acceptance evidence.

## Ownership boundaries

- Identity owns the tables in `identity_service/schema`; it does not read or write the Market database or SaaS PostgreSQL database.
- SaaS keeps a same-UUID user projection because subscriptions, payments, wallet, support and CRM tables have local foreign keys to `users(id)`. The projection is not an authentication authority and password hashes are never copied into it.
- SaaS validates the presented session token with Identity over the private service network on each authenticated request. Permission checks use the permission claims returned by Identity. An Identity timeout or invalid service credential fails closed.
- Existing UUIDs, password hashes, active sessions, MFA seeds, roles and acceptance evidence can be copied from the current SaaS database with the explicit one-time migration utility. Its default mode is dry-run and the source database is read-only. MFA ciphertext is re-encrypted with a dedicated Identity key; temporary OTP challenges and SaaS business notifications are intentionally not copied.

## HTTP contract

Public (routed by the Edge): `POST /auth/register`, `POST /auth/login`, `POST /auth/phone/request`, `POST /auth/phone/verify`, `GET /auth/me`, `POST /auth/logout`, and `/auth/mfa/{status,setup,confirm,verify}`.

Private service contract (requires `X-Identity-Service-Token`):
- `POST /internal/introspect` with the caller's opaque user session token.
- `POST /internal/users/{id}/status` and `PUT /internal/users/{id}/roles/{role}`; both also require the acting user's session token and Identity-side permission checks.
- `GET /internal/sessions`, `DELETE /internal/sessions/{id}`, `POST /internal/sessions/revoke-others` and `PATCH /internal/users/me/profile`; all are scoped to the session owner.

No public route trusts user IDs, roles or permissions supplied by a browser. The service token is distinct from all database credentials and user session tokens.

## Deployment and migration

Set `IDENTITY_DATABASE_URL`, `IDENTITY_EXPECTED_DATABASE_NAME`, `IDENTITY_SERVICE_TOKEN`, `IDENTITY_OTP_HMAC_SECRET`, `IDENTITY_OUTBOX_FERNET_KEY`, `IDENTITY_ADMIN_MFA_FERNET_KEY`, and reviewed legal versions in a secret manager. Production remote PostgreSQL connections require `sslmode=verify-full`.

Apply schema using `python scripts/manage_identity_migrations.py --apply`. For an existing installation, first create an empty dedicated Identity database and run `python scripts/migrate_saas_identity.py` (dry-run), review its counts, then pass `--apply`. Only after a successful migration should production Edge routing and `IDENTITY_SERVICE_URL` be enabled. The migration tool refuses identical source/target databases and refuses a non-empty target.

The old SaaS auth routes remain as a local/rollback compatibility path when `IDENTITY_SERVICE_URL` is absent. They are not the production public auth ingress once the Edge routes `/api/v1/auth/*` to Identity.


The one-time data import preserves user UUIDs, password hashes, current session
hashes, roles, permission relationships and legal acceptance evidence. Enrolled
TOTP secrets are re-encrypted from the former SaaS MFA key to the dedicated
Identity MFA key. Ephemeral phone OTP challenges and the SaaS notification
outbox are intentionally excluded: users request fresh OTP codes after
cutover, while business notification delivery remains owned by SaaS.
