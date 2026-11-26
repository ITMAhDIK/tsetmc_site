# Tloomberg Authentication Contract

## Purpose

The GitHub Pages login screen is a static client. It must never decide a
user's role, verify an OTP, mint a session, or grant administrative access.
Those decisions belong to a separately hosted HTTPS authentication API.

## Frontend contract

The public page reads the API origin from:
`<meta name="tloomberg-auth-api" content="https://AUTH-API-HOST">`

It calls these same-origin-or-CORS-enabled endpoints with
`credentials: "include"`:

- `POST /api/v1/auth/otp/request`
  - Login request: `{"phone_e164":"+989...","purpose":"login"}`.
  - Registration request: `{"phone_e164":"+989...","purpose":"register","terms_accepted":true,"terms_version":"published-version"}`.
  - Response: `{"ok":true,"challenge_id":"opaque-id","expires_in":300}`.
  - Registration must be rejected unless the current published terms version
    is active and explicitly accepted. The frontend placeholder
    `pending-owner-content` is not a publishable terms version.
- `POST /api/v1/auth/otp/verify`
  - Registration adds `purpose":"register","terms_accepted":true,"terms_version":"published-version"`
    to the OTP verification body; login uses `purpose":"login"`.
  - Response: `{"user":{"phone_e164":"+989...","public_id":"TL-...","username":"..."},"role":"user","permissions":[]}`.
  - The response must also set a Secure, HttpOnly, SameSite cookie. Do not return a
    long-lived bearer token to browser JavaScript.
- `GET /api/v1/auth/me`
  - Returns `{"user":{"public_id":"TL-...","username":"...","phone_e164":"..."},"role":"user"}`
    for the authenticated session. The public ID is safe to display in the profile.
- `GET /api/v1/auth/google/start?intent=login|register`
  - Starts Google OAuth with server-generated state and PKCE. For registration,
    the frontend sends the accepted terms version; the server stores it in the
    signed OAuth state and rejects account creation while final terms are unpublished.
- `GET /api/v1/auth/google/callback`
  - Validates state, PKCE, issuer, audience, nonce and Google identity; creates
    or links the account only under the verified identity-linking policy, then
    redirects to the fixed `APP_ORIGIN/saas/?oauth=success`.
- `POST /api/v1/auth/logout`
  - Revokes the server-side session and clears the cookie.

## Superadmin bootstrap

The superadmin number must be configured only in the private backend environment
as `SUPERADMIN_PHONE_E164`. Do not commit the actual number, publish it in
HTML/JavaScript, or store it in GitHub Pages configuration.

After a valid OTP has been verified, the backend normalizes the verified number
to E.164 and compares it with `SUPERADMIN_PHONE_E164`. Only that exact verified
identity receives the `superadmin` role. Any other first-time verified phone
receives the ordinary `user` role. The client must not be able to submit a
role, user ID, or permission to influence this decision. Superadmin provisioning
is an out-of-band operation, never a public signup option.

## Security requirements

- Use the independent SaaS PostgreSQL database, never the market-data DB.
- Hash OTPs at rest; six digits, five-minute expiry, one-time use, max five
  verification attempts, resend cooldown and per-phone/per-IP rate limits.
- Use cryptographically secure OTP generation; never log OTP values.
- Use opaque, hashed server-side session tokens; rotate on login and revoke on
  logout. Cookies must be Secure, HttpOnly and SameSite.
- Protect state-changing endpoints against CSRF and validate Origin.
- Enforce RBAC on every protected API, export and admin operation. Hiding a
  menu in the browser is not authorization.
- Never expose the existing unauthenticated local ingestion/admin API to the
  public Internet. It is not a SaaS authentication backend.
- Restrict CORS to the production Tloomberg origin; do not use wildcard CORS
  with credentialed requests.
- Configure secrets in the hosting provider's secret manager, not in source
  control or GitHub Pages.

## Required private backend settings

- `SAAS_DATABASE_URL`: dedicated PostgreSQL URL.
- `APP_SECRET_KEY`: at least 32 random bytes.
- `SUPERADMIN_PHONE_E164`: the owner's verified number.
- `SMS_PROVIDER`: the selected SMS gateway adapter.
- `SMS_API_KEY`: provider credential (or the provider's required credential set).
- `APP_ORIGIN`: production frontend origin allowed by CORS/CSRF.

The live SMS integration and API hosting remain pending until the SMS gateway,
PostgreSQL host and backend deployment have been provisioned and tested.

## GitHub Pages demo mode (temporary)

When the auth API meta value is blank, the public login screen runs in a
clearly labelled UI-only demo: it accepts any four-digit value and does not
send SMS, create a server session, or authorize API calls. The screen identifies
the configured owner's test identity by a client-side SHA-256 comparison only
to preview the superadmin label. This is intentionally NOT an authentication
mechanism: hashes embedded in public JavaScript are not secrets. Do not expose
administrative endpoints or data based on this demo role. Remove/disable demo
mode only after the real backend and SMS provider have been configured and
verified. In the live API mode, the server remains solely responsible for role
assignment and session creation.


## Terms and registration consent

The registration view requires an explicit checkbox before phone OTP
registration or Google registration can begin. The linked legal page is
`/saas/terms.html`. It currently contains a placeholder because the owner has
not yet supplied the final legal text. Until a version is published, the
backend must reject account creation and must not record acceptance of the
placeholder as acceptance of final terms. Store accepted terms version,
timestamp and user ID as an auditable consent record.
