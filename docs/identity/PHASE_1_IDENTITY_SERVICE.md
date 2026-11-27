# Phase 1 — Identity Service

**Status:** implementation committed on the architecture-stabilization branch; production cutover is deliberately not performed by CI or by this change.

## Goal and boundary

Extract authentication and authorization into the first independently deployable business service:

- Registration and login (Argon2id username/password and phone OTP).
- HMAC-protected, throttled OTP challenges and encrypted SMS outbox.
- Opaque, hashed sessions with expiry, revocation and session management.
- TOTP MFA for privileged users, encrypted MFA secrets and replay protection.
- Role/permission catalog and user-role assignment.
- Immutable versioned terms/privacy acceptance evidence.
- Private principal introspection for other services.

Identity owns these facts in its own PostgreSQL database. It does not read Market SQLite, SaaS PostgreSQL or any product database.

## Database ownership and compatibility

The Identity schema is in `identity_service/schema/001_identity_core.sql`.
The runtime database user is separate from the migration owner. The migration
runner verifies the expected database name, uses an advisory lock and records
per-migration SHA-256 checksums. Remote migration connections must use
`sslmode=verify-full`.

SaaS still has a `users` table because subscriptions, payment orders, wallet,
support, referrals and CRM hold local foreign keys to `users(id)`. That table
becomes a **projection**, not an authentication authority. SaaS upserts the
same UUID and non-secret profile/status fields after successful Identity
introspection, clears `password_hash`, and synchronizes role codes only to
preserve local relations. The SaaS database continues to own plans,
subscriptions, wallet and product entitlements. No cross-database FK or join
is introduced.

## HTTP boundary

The same-origin Edge forwards `/api/v1/auth/*` to Identity after removing the
`/api/v1` prefix. Other `/api/v1/*` endpoints continue to SaaS.

Public Identity routes:

| Method | Route | Purpose |
|---|---|---|
| POST | `/auth/register` | Password registration with current terms/privacy acceptance |
| POST | `/auth/login` | Password login |
| POST | `/auth/phone/request` | Queue a time-limited phone OTP |
| POST | `/auth/phone/verify` | Consume OTP and create a session |
| GET | `/auth/me` | Current Identity user, roles, permissions and MFA status |
| POST | `/auth/logout` | Revoke current session |
| GET/POST | `/auth/mfa/status`, `/setup`, `/confirm`, `/verify` | Privileged TOTP lifecycle |

Private routes require `X-Identity-Service-Token`. Where the caller acts for
a user, it must also send that user's original opaque session in
`X-Identity-User-Token`. Identity validates that session and performs the
permission check itself; a caller-supplied UUID or role is never sufficient.

- `POST /internal/introspect`: validate a user session and return a minimal principal containing user profile, UUID, roles, permissions and MFA state. It never returns password hashes, token hashes or the raw session token.
- `GET /internal/sessions`, `DELETE /internal/sessions/{id}`, `POST /internal/sessions/revoke-others`: session-owner scoped operations.
- `PATCH /internal/users/me/profile`: update the current user's display name.
- `POST /internal/users/{id}/status`, `PUT /internal/users/{id}/roles/{role}`: admin changes authorized by Identity permissions and recent session-bound MFA.

SaaS calls introspection on each authenticated request. If Identity is
unavailable, SaaS returns 503; it never silently falls back to its old local
session tables when the Identity URL is configured. Invalid or revoked
sessions return 401. API authorization helpers use the Identity permission
claim instead of querying local RBAC tables. Paid-feature entitlements remain
resolved from SaaS subscriptions.

## Data migration from the current SaaS database

The one-time migration is `scripts/migrate_saas_identity.py`.

1. Provision a *separate empty* Identity PostgreSQL database and apply Identity
   migrations with `python scripts/manage_identity_migrations.py --apply`.
2. Set `IDENTITY_IMPORT_SOURCE_DATABASE_URL` / `IDENTITY_IMPORT_SOURCE_DATABASE_NAME`
   to a read-only connection to the existing SaaS database, and
   `IDENTITY_MIGRATION_DATABASE_URL` / `IDENTITY_EXPECTED_DATABASE_NAME`
   to the Identity migration-owner database.
3. Run `python scripts/migrate_saas_identity.py` first. Default is dry-run;
   review the reported counts.
4. Run `python scripts/migrate_saas_identity.py --apply` only after the
   snapshot/backup is approved and both database identities are verified.
5. Pause SaaS writes during the cutover window. Set
   `IDENTITY_CUTOVER_SOURCE_DATABASE_URL` to a separate, time-limited
   maintenance connection with permission to retire credentials in the SaaS
   database (do not reuse the importer's read-only credential). Confirm
   Identity user counts and sample password login/MFA state, then run
   `python scripts/finalize_saas_identity_cutover.py` (dry-run).
6. Only after parity checks pass, run it with `--apply`. This retires SaaS
   password hashes, session token hashes, MFA ciphertext, email/phone challenge
   secrets and stale OTP outbox payloads while preserving the non-secret local
   UUID/user projection and all business foreign keys.
7. Start the Identity/SaaS API and Edge route switch. Future registrations,
   logins and OTP challenges are now created only in Identity.

The importer refuses identical source/target DSNs and database names, refuses
a target with existing user-owned data, and sets the source connection
read-only. It does not transfer `phone_verifications` or any `notification_outbox`
rows, since OTP challenges and their old-key ciphertext must not cross the
cutover. Business notifications remain in SaaS. Existing MFA seeds are decrypted using a temporary
`IDENTITY_IMPORT_SOURCE_MFA_FERNET_KEY` and re-encrypted using the new
`IDENTITY_ADMIN_MFA_FERNET_KEY`; the source ciphertext is never changed. It preserves UUIDs, password hashes, active
session token hashes, verified profile fields, role and permission
relationships, re-encrypted MFA ciphertext/counters, legal acceptance
evidence and audit records. Temporary phone OTP challenges are intentionally
not copied because the HMAC key changes at cutover; all users request fresh
codes. SaaS business notifications remain in the SaaS database and are never
copied into Identity's outbox. Role/permission links are mapped
by stable codes rather than serial IDs. The tool does not delete or update
source rows.

## Deployment

- `Dockerfile.identity` builds a separate non-root image (UID/GID 10002).
- `docker-compose.saas.production.yml` adds `identity-migrate`,
  `identity-api`, and `identity-sms-worker on the private backend network.
- The SaaS API receives only `IDENTITY_SERVICE_URL` and the private service
  token; the Identity database URL is not provided to SaaS.
- Caddy routes browser auth paths to Identity and all other application APIs
  to SaaS.
- Local development has a separate Identity PostgreSQL container on localhost
  port 55433 and Identity API on localhost port 58001.

Required production secrets include a distinct service token, OTP HMAC
secret, outbox Fernet key, MFA Fernet key, database credentials and legal
version values. Outbox and MFA encryption keys must not be reused. Database
URLs and secrets are injected from the protected environment, not committed.

## Validation included

The Identity-specific PostgreSQL/API suite covers schema readiness, service
token enforcement, principal field minimization, invalid/revoked sessions,
login/password verification, registration fail-closed behavior and strict
session-owner isolation for session management. The main CI workflow creates
a second disposable PostgreSQL database, applies Identity migrations and runs
these tests independently from the SaaS integration suite. The security
workflow compiles the service, scans it with Bandit, builds the non-root image
and validates the production Compose/Caddy configuration.

The existing SaaS test/development path remains available when
`IDENTITY_SERVICE_URL` is unset. After a production cutover, rollback to the
legacy auth path requires restoring the pre-cutover SaaS database snapshot or
performing a separately reviewed reverse credential/session synchronization;
simply unsetting the URL is not a supported production rollback.

## Explicitly not done by this phase

No production database was copied, no secrets were installed on a production
host, no public route was switched on a live domain and no deployment was
performed. Production rollout still needs staging migration rehearsal,
account-count and sample-login reconciliation, real SMS verification,
a dedicated encrypted Identity database backup/restore workflow and verified
recovery test (the existing SaaS backup does not cover this separate database),
service-token rotation and network-policy review, load/soak tests, independent
penetration/security review and a rehearsed rollback. Google OAuth,
password-reset flows, cross-provider identity linking and email-verification
user flows remain outside this phase.
