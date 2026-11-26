# SaaS authentication service

FastAPI authentication API for the dedicated SaaS PostgreSQL database.
`SAAS_DATABASE_URL` must never point to the Market or collector database.

## Current capabilities

- Username/password registration and login using Argon2id.
- Opaque server-side sessions; only SHA-256 token hashes are stored.
- Production session cookies are `HttpOnly; Secure; SameSite=Lax`.
- Phone OTP is HMAC-protected, short-lived, rate-limited, single-use and
  delivered from an encrypted outbox by `scripts/saas_sms_worker.py`.
- The configured bootstrap phone receives superadmin only after verified OTP;
  other new phone signups receive the ordinary user role.
- Privileged accounts must enroll TOTP. The seed is Fernet-encrypted with
  `SAAS_ADMIN_MFA_FERNET_KEY`, separate from OTP/outbox secrets. Verification
  is session-bound, lasts at most 12 hours and rejects replayed counters.
- Password/phone signup requires the exact terms and privacy versions from the
  protected API environment. Phone signup versions are bound to the OTP
  challenge; legal acceptance evidence is append-only.
- Prometheus metrics use a separate bearer token and low-cardinality labels.

## HTTP surface

- `POST /auth/register`, `POST /auth/login`
- `POST /auth/phone/request`, `POST /auth/phone/verify`
- `GET /auth/me`, `POST /auth/logout`
- `GET /auth/mfa/status`, `POST /auth/mfa/setup`,
  `POST /auth/mfa/confirm`, `POST /auth/mfa/verify`
- `GET /health`, `GET /ready`
- `GET /internal/metrics` — private bearer-protected route; public Caddy
  deliberately does not proxy it.

Admin, support and analytics endpoints are mounted from separate routers.

## Same-origin browser deployment

Production Edge serves login/admin/legal pages and the API from one HTTPS
origin. Browser API requests use `/api/v1`; Caddy strips that prefix before
forwarding to FastAPI. This preserves first-party session cookies. The
GitHub Pages copy is a preview only: it refuses API calls on `.github.io`
and demo authentication is disabled.

Google OAuth, identity linking and account recovery are not active browser
flows. Do not present them as supported authentication methods.

## Protected settings and legal release gate

Use independent random values for:

- `SAAS_OTP_HMAC_SECRET`
- `SAAS_OUTBOX_FERNET_KEY`
- `SAAS_ADMIN_MFA_FERNET_KEY`
- `SAAS_METRICS_TOKEN`

Configure dedicated database credentials, verified bootstrap phone, exact
trusted host/origin allow-lists, SMS provider credentials and Prometheus
multiprocess settings through a secret manager. Never commit real values.

Do not set `SAAS_TERMS_VERSION` or `SAAS_PRIVACY_VERSION` until the owner and
legal reviewer approve the text and immutable versions. The API fails closed
while either is absent or a placeholder.

## Migrations and verification

Apply and checksum-verify migrations 001–013 using
`scripts/manage_saas_migrations.py`. CI uses only a disposable PostgreSQL
service. Latest verified branch commit:
`fb0a15fa6c627fa3fd075ccc56e31efdf50f4c34` — **373 passed, 0 skipped**,
with all SaaS security workflow jobs successful. This is not a production
deployment or independent penetration test.
