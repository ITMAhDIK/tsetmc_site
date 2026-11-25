# SaaS authentication service — Phase 2

Independent FastAPI service. SAAS_DATABASE_URL must point only to the dedicated
SaaS PostgreSQL database, never the market-data database.

## Included
- Username/password registration and login using Argon2id.
- Random opaque sessions; only SHA-256 token hashes are stored in PostgreSQL.
- HttpOnly, SameSite=Lax cookies (Secure by default), expiry and revocation.
- Phone OTP challenge storage with five-minute expiry, one-time use and attempt cap.
- OTP values are HMAC-SHA256 hashed using SAAS_OTP_HMAC_SECRET and SAAS_OUTBOX_FERNET_KEY.
- OTP delivery is queued to notification_outbox; no SMS provider is called inline.
- Minimal /auth/me and /health endpoints.

## Not yet integrated
Google OAuth, identity linking, password reset, SMS provider delivery,
distributed rate limiting, CSRF protection, superadmin bootstrap command,
authorization/entitlement middleware and production deployment remain.
Public registration never grants administrative roles.

## Local setup
Install requirements-saas.txt and apply saas_core/schema/001_initial.sql to a
fresh dedicated SaaS database. Set SAAS_DATABASE_URL, SAAS_OTP_HMAC_SECRET
(at least 32 random characters), and SAAS_COOKIE_SECURE=true outside localhost.

Run: uvicorn saas_core.auth.api:app --host 127.0.0.1 --port 8010

The SMS outbox remains pending until a separately configured worker exists.
Do not expose publicly before rate limiting, CSRF, CORS allow-listing,
monitoring and provider delivery are deployed.
