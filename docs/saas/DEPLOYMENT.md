# SaaS production deployment and browser session origin

## One public origin for the browser and API

The production edge serves the Persian login page, admin page, legal pages and
FastAPI endpoints from the same HTTPS origin:

- `https://<SAAS_DOMAIN>/` — login / registration
- `https://<SAAS_DOMAIN>/admin.html` — superadmin console
- `https://<SAAS_DOMAIN>/terms.html` — terms placeholder until owner approval
- `https://<SAAS_DOMAIN>/privacy.html` — privacy placeholder until owner approval
- `https://<SAAS_DOMAIN>/api/v1/*` — proxied to the private FastAPI container

The static HTML uses `same-origin` as its API base. Caddy removes the
`/api/v1` prefix before forwarding to FastAPI. This keeps the browser session
cookie first-party and compatible with its `HttpOnly; Secure; SameSite=Lax`
policy. Do not deploy the login/admin HTML on a separate cross-site origin
while relying on the API cookie; third-party cookie blocking can break the
authenticated browser flow.

The main market website can remain hosted separately. Link its account/login
entry point to the SaaS origin above. Configure `SAAS_ALLOWED_ORIGINS` with
the exact SaaS browser origin and any separately approved first-party frontend
origins that call the API.

## Required protected settings

Before deployment, provision DNS and HTTPS for `SAAS_DOMAIN`, point it to the
edge host, and set `SAAS_TRUSTED_HOSTS` and `SAAS_ALLOWED_ORIGINS` to that
exact hostname/origin. Use an immutable image digest in `SAAS_API_IMAGE` and
`SAAS_EDGE_IMAGE`; do not deploy the example tags or placeholders.

Generate independent secrets for OTP HMAC, SMS outbox encryption, privileged
TOTP encryption, and the Prometheus scrape token. Store them in the host's
secret store, not in Git. The Prometheus token file must be readable by the
Prometheus container only.

Set the verified bootstrap superadmin phone in protected environment. Enroll
TOTP on the first privileged login. The edge does not expose
`/internal/metrics`; Prometheus scrapes that endpoint only on the private
Compose network.

## Legal release gate

The checked-in terms and privacy pages are explicitly placeholders, not final
legal documents. Do not set `SAAS_TERMS_VERSION` or
`SAAS_PRIVACY_VERSION` to arbitrary strings. After owner/legal approval:

1. Replace the placeholder paragraphs with the approved documents.
2. Assign immutable version identifiers, for example a publication date plus
   revision.
3. Set the exact matching versions in the protected API environment.
4. Run the OTP signup integration tests against the new versions.

The API fails closed while either version is empty or a placeholder. It also
stores the versions on the OTP challenge and refuses to relabel an in-flight
challenge during verification.

## Deploy and verify

Validate the production Compose files first. Apply migrations with the
separate migration-owner credential. Start the stack using the approved image
digests. Confirm:

- `/health` and `/ready` succeed inside the API network.
- The browser can request and verify a six-digit OTP from the same HTTPS origin.
- A normal user cannot open any admin route.
- A superadmin is required to enroll and verify TOTP before the console opens.
- TOTP replay and expired MFA sessions are rejected.
- Signup remains blocked until approved legal versions are configured.
- Prometheus is private and alert delivery has been exercised.

A successful CI run does not provision DNS, SMS provider credentials,
PostgreSQL, secret storage, off-host object-lock retention, Alertmanager or an
on-call rotation.
