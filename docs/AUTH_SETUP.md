# Phone OTP login and bootstrap superadmin

## What the public account page does

The account page in `docs/index.html` now has a real phone + six-digit OTP flow:

1. The browser posts an E.164 phone number to `POST /auth/phone/request`.
2. The API creates a short-lived, HMAC-protected challenge and adds an encrypted SMS job to `notification_outbox`.
3. The dedicated `saas-sms-worker` claims the job, decrypts the destination and OTP, and dispatches it through the configured SMS provider.
4. The browser posts the phone, OTP and purpose to `POST /auth/phone/verify`.
5. After successful verification the backend creates a secure session cookie and `GET /auth/me` returns the server-derived role.

A previously unseen, successfully verified phone number becomes a standard user. The same login flow therefore also creates a new account on first use.

## Superadmin assignment

Exactly one bootstrap number can be configured on the backend with:

```dotenv
SAAS_SUPERADMIN_PHONE_E164=+98...
```

Set the full E.164 number in the protected runtime secret store for the API container. Do not add the actual number to source code, GitHub Pages, a public config file, frontend JavaScript, a test fixture or a committed environment file.

After successful OTP verification, the backend normalizes the verified phone and compares it against this server-side setting. Only a match receives the `superadmin` role. All other verified numbers receive only the `user` role. The browser does not send a role, and the API ignores any attempt to select one. The initial grant is recorded in `admin_audit_logs`.

Production API startup fails closed if the bootstrap phone is missing or malformed. If the environment variable is absent in development/test, all phone-verified accounts remain standard users.

## SMS provider configuration

The worker currently supports Kavenegar's HTTPS form-encoded `sms/send.json` endpoint. Kavenegar's official example uses the `receptor`, `sender` and `message` fields:
https://kavenegar.github.io/kavenegar_fa/

Configure these values in the protected server environment, not in GitHub Pages:

```dotenv
SMS_PROVIDER=kavenegar
SMS_API_KEY=<secret-from-your-kavenegar-account>
SMS_SENDER=<approved-sender-line>
SAAS_OUTBOX_FERNET_KEY=<same-protected-key-used-by-the-API>
SAAS_DATABASE_URL=<low-privilege-SaaS-database-URL>
SAAS_EXPECTED_DATABASE_NAME=<dedicated-SaaS-database-name>
SAAS_NOTIFICATION_POLL_SECONDS=2
```

The production Compose file starts `saas-sms-worker` automatically after migrations. It reads only encrypted outbox values, sends the OTP via HTTPS, marks accepted messages as sent and retries failures with bounded exponential backoff. Logs contain aggregate counters and error classes only; they never include the API key, phone, OTP, message or provider response body.

A successful provider API response means the provider accepted the message, not a guarantee that the mobile carrier delivered it. Check delivery status in the provider's account when diagnosing a missing message.

## Domain, CORS and session-cookie requirements

The API sets an HttpOnly, Secure, SameSite=Lax session cookie. The browser uses `credentials: include`. For reliable browser authentication, use a custom domain for the Pages site and put the API on the same site, for example:

- Frontend: `https://app.example.com`
- API: `https://api.example.com`

Set `SAAS_ALLOWED_ORIGINS=https://app.example.com` and `SAAS_TRUSTED_HOSTS=api.example.com` in protected server configuration. Wildcard origins are not supported. The default `*.github.io` host and an unrelated API domain are not a suitable final cookie topology for this SameSite=Lax setup; configure a custom Pages domain (or a same-origin reverse proxy) before expecting the login cookie to persist in browsers.

Set `docs/analytics-config.js` `apiBase` to the final HTTPS API origin only after the backend is deployed, DNS/TLS and CORS are configured, and the API/database are ready. The value is a public URL, not a secret.

## Deployment order and smoke test

1. Provision a dedicated SaaS PostgreSQL database and separate runtime/migration roles.
2. Create a Kavenegar account, activate an approved sender line and store the API key in the server secret manager.
3. Put `SAAS_SUPERADMIN_PHONE_E164`, OTP HMAC secret, Fernet key, SMS settings, database URLs, trusted hosts and allowed origins in the protected production environment.
4. Build and deploy the immutable API image. The migration service applies migrations `001` through `010`; the API readiness endpoint checks the notification worker columns.
5. Confirm the SMS worker is running and can reach Kavenegar over HTTPS.
6. Set the public API origin in `docs/analytics-config.js` and deploy the Pages frontend on its custom domain.
7. Open Account, enter the bootstrap number, request an OTP, verify it and confirm `/auth/me` reports `is_superadmin=true`.
8. Sign out and verify a different number; it must report `is_superadmin=false` and have only the `user` role.

Do not test against a production phone until the SMS provider account, sender line, billing/quota and server environment have been configured. GitHub Actions uses only synthetic phone numbers and fake SMS senders.

## Current limitation

The repository now contains the UI, server role mapping and Kavenegar worker implementation, but no production API host, database, Kavenegar API key, sender line or protected runtime secrets have been provisioned from this change. The public Pages configuration therefore remains disabled by default. No OTP has been sent to a real phone yet.
