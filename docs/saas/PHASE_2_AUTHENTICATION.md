# Phase 2 — Authentication and authorization

## Implemented in this branch
- Username/password registration and login using Argon2id.
- Random 256-bit opaque session tokens; only SHA-256 token digests are persisted.
- HttpOnly, SameSite=Lax cookie, Secure by default, session expiry and revocation.
- Phone OTP challenges with five-minute expiry, one-time use, HMAC-SHA256 hashes
  and a persisted five-attempt ceiling.
- OTP delivery is queued to the SaaS notification outbox.
- Default user role, profile identity, preferences and wallet account are created
  in the same transaction as account registration.
- Permission checks resolve role_permissions on the server.
- Subscription entitlements are resolved separately from administrative roles.
- No public endpoint can grant administrative roles.

## Not production complete
Google OAuth, verified identity linking, password reset, SMS provider worker,
distributed rate limiting, CSRF/CORS policy, superadmin bootstrap command,
automated integration tests against PostgreSQL, deployment and security review
remain open. OTP requests are currently not rate-limited by IP/phone at the API
edge; do not expose this service publicly until rate limiting is deployed.
The SMS outbox has no provider worker yet, so phone verification codes are
queued but not delivered in this repository version.

## Local execution
Install requirements-saas.txt, provision a dedicated SaaS PostgreSQL database,
and apply schema/001_initial.sql. Configure SAAS_DATABASE_URL and
SAAS_OTP_HMAC_SECRET (at least 32 characters). Set SAAS_COOKIE_SECURE=true
outside localhost HTTPS. Run:
uvicorn saas_core.auth.api:app --host 127.0.0.1 --port 8010

## API
- GET /health
- POST /auth/register
- POST /auth/login
- POST /auth/phone/request
- POST /auth/phone/verify
- GET /auth/me
- POST /auth/logout

Session is accepted from the HttpOnly cookie or an Authorization Bearer token.
Market-data database and APIs remain separate; this service never opens them.
