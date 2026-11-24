# Membership and privacy deployment guide

This branch keeps account and billing state separate from both the read-only TSETMC source database and the analytics sidecar.

## Persistent files

Keep the following files on a persistent server volume and include them in the normal backup plan:

- `site_accounts.sqlite` — users, password hashes, sessions, subscriptions, orders, wallet ledger, referrals and tickets.
- `site_data.sqlite` — existing analytics sidecar (managed by the existing application).

Do not put either SQLite file, the source holdings database, instrument registries, raw exports or workbook directories under a public static path. The HTTP handler now serves only an explicit allow-list of frontend assets. The source database remains read-only.

## Required environment variables

Set these on the server's service manager or protected environment file, not in source control:

| Variable | Purpose |
| --- | --- |
| `SITE_PUBLIC_BASE_URL` | Canonical public HTTPS origin, e.g. `https://example.com`. Used for secure cookies and payment callback URLs. |
| `SITE_BOOTSTRAP_ADMIN_KEY` | Long random one-time key for initial Super Admin activation. Remove or rotate it after initial setup. |
| `GOOGLE_CLIENT_ID` | Google OAuth client ID. |
| `GOOGLE_CLIENT_SECRET` | Google OAuth client secret. |
| `GOOGLE_REDIRECT_URI` | Exact registered callback URL: `https://example.com/api/v1/auth/google/callback`. |
| `SITE_SMS_API_URL` | HTTPS endpoint of the chosen SMS provider adapter. |
| `SITE_SMS_API_TOKEN` | Secret bearer token for the SMS adapter. |
| `ZARINPAL_MERCHANT_ID` | ZarinPal merchant identifier. |
| `ZARINPAL_SANDBOX` | Set to `1` for sandbox testing; set to `0` for production. |

### SMS adapter contract

The service sends an HTTPS JSON POST with a bearer token and these fields:

```json
{
  "phone": "+989121234567",
  "code": "123456",
  "message": "کد ورود ره‌نگار بورس: 123456"
}
```

The configured adapter must deliver the code to the requested phone and return a successful JSON response. This is intentionally provider-neutral. Do not use a fake development code in production.

### Google OAuth

Create a Web application OAuth client. Register the exact `GOOGLE_REDIRECT_URI` above in Google's authorized redirect URIs. The implementation uses Authorization Code flow with PKCE, state, nonce, verified email and token audience checks.

### Payment gateway

The checkout implementation uses ZarinPal v4 request/verify endpoints. Amounts are stored and displayed in Toman; the gateway amount is converted to Rial (Toman × 10). The callback must use the same public HTTPS domain configured in `SITE_PUBLIC_BASE_URL`.

Before accepting real payments:
1. Test request, cancellation, successful verification and duplicate callbacks in sandbox.
2. Confirm the merchant account's settlement rules and refund workflow.
3. Confirm the public callback URL is reachable through the reverse proxy.
4. Check wallet-debit rollback on failed/abandoned gateway requests.

A production gateway credential is not included in this repository. Until it is configured and end-to-end tested, checkout must be treated as unavailable.

The public group dashboard uses the official TSETMC instrument-sector registry in `data/instrument_sectors.json`. The checked-in snapshot on this branch contains 724 instrument mappings, so it must be treated as partial until the server-side synchronization job has completed and coverage is reviewed. The dashboard reports coverage and does not imply that missing instruments have been classified.

To refresh the registry on the server (do not run this against a client laptop or against a public upload), provide the actual local source database path:

```powershell
python tools/sync_instrument_sectors.py --db "<absolute path to tsetmc_holdings.sqlite>" --output "data/instrument_sectors.json"
```

The tool reads the source database read-only and writes the JSON registry atomically/resumably. Its defaults are 0.12 seconds between requests, two retries, and a checkpoint every 25 instruments. Use `--limit` only for a small pilot; do not interpret a limited run as complete. This is a network-backed registry refresh, separate from account or billing setup.

## First Super Admin setup

1. Deploy the new branch to a staging environment over HTTPS.
2. Set `SITE_BOOTSTRAP_ADMIN_KEY` to a unique random secret known only to the owner.
3. Register the owner's ordinary account using the normal registration screen.
4. Log in and open **حساب و اشتراک**.
5. Expand **راه‌اندازی اولیه سوپرادمین** and submit the bootstrap key.
6. The bootstrap operation is one-time and invalidates the current session. Log in again.
7. Add admin and support accounts from **پنل سوپرادمین**. Give each staff member a temporary password through a secure channel. It must be changed on first login.
8. Remove the bootstrap key from the server environment and restart the application after setup.

Never set the Super Admin role manually in a public registration request. The server does not accept role selection during member registration.

## Roles

- **member** — personal profile, subscription, wallet and own support-ticket creation/replies.
- **support** — support inbox, replies and ticket assignment/status workflow; no user/role administration.
- **admin** — staff/ticket overview and support operations.
- **super_admin** — admin capabilities plus creating staff, changing staff roles and accessing the separate infrastructure administration page.

The last active Super Admin cannot be demoted through the staff role endpoint.

## Authentication and session behaviour

- Password login accepts username, public user ID, email or phone.
- Remember-me sessions last 30 days; ordinary sessions last 12 hours.
- Session tokens are stored as SHA-256 hashes, and cookies are HttpOnly and SameSite=Lax. The Secure flag is set when the canonical origin is HTTPS.
- Write requests require the session CSRF token in `X-CSRF-Token`.
- Phone login uses five-minute, six-digit one-time codes with per-number request and attempt limits.
- New phone/Google users receive a one-time three-day trial. Ordinary username/password registrations do not receive automatic phone verification.

## Privacy boundary

The public pages are intentionally limited to market/sector aggregates and stock price analysis. These paths are disabled on the public API:

- shareholder search and observation-history,
- all investor portfolio endpoints,
- per-stock shareholder records,
- raw ownership series and data-quality records,
- legacy screener query/export.

Public market watch responses omit share counts and all ownership/holder columns. The public stock detail response omits company-history and owner information. Corporate-action markers omit old/new share totals and ratios. The static-file handler does not serve SQLite files, JSON registries, spreadsheets, source files or arbitrary files from the project directory.

Do not re-enable a raw endpoint by hiding its frontend tab. If a new analytical feature needs source data, compute the result on the server and return only the approved aggregate fields.

## Subscription catalogue currently seeded

Prices are in Toman:

| Plan | Duration | Price |
| --- | ---: | ---: |
| Professional Monthly | 30 days | 2,790,000 |
| Professional Quarterly | 90 days | 7,990,000 |
| Professional Semiannual | 180 days | 15,490,000 |
| Professional Annual | 365 days | 28,900,000 |

The `WELCOME10` promotion gives 10% off the first successful purchase (limited to 2,000 redemptions). The subscription is extended from the current expiry date rather than overwriting unused paid time.

### Market positioning reference checked on 2026-11-24

Publicly listed comparison prices observed during implementation:

- NabzBours lists monthly tiers at 99,000, 199,000 and 299,000 Toman: https://nabzbours.ir/
- A recent overview of Rahavard365 lists Silver at 430,000 and Gold at 570,000 Toman per month, and Platinum at 2,535,000 Toman: https://iranrebate.com/education/rahavard365/
- BazaarBasheh lists a 2,000,000 Toman monthly professional plan: https://bazaarbasheh.ir/accounts/subscribe/

The 2,790,000 Toman monthly price is deliberately positioned slightly above the cited 2,535,000 Toman Platinum monthly reference, and above the cited 2,000,000 Toman monthly professional plan. The annual price also sits above the cited 18,000,000 Toman annual professional package. This is a high-end positioning decision, not evidence that the product already matches every competitor's feature breadth. Review willingness to pay and feature parity before charging real customers.

## Referral reward

Every user has a unique referral code and a shareable `/?ref=CODE` link. The inviter receives 100,000 Toman in wallet credit only after the invited user's first successful paid subscription. Signup alone does not pay a reward. A unique ledger reference prevents duplicate credit from repeated payment callbacks. The current reward is a site credit, not a cash withdrawal promise.

## Charts

The app continues to use the repository's existing TradingView Lightweight Charts 4.2.3 integration. The UI now adds price ranges, SMA/EMA, Bollinger bands, volume, log/percentage scale, fullscreen, drawing tools, Fibonacci retracement and per-symbol drawing persistence.

Lightweight Charts is the free open-source chart renderer; it does not include a complete TradingView interface or built-in indicator catalogue. The chart toolbar and indicators in this branch are custom code. TradingView publishes separate Advanced Charts/Trading Platform products with materially broader prebuilt capabilities and licensing conditions. See:
- https://www.tradingview.com/free-charting-libraries/
- https://tradingview.github.io/lightweight-charts/plugins
- https://tradingview.github.io/lightweight-charts/docs/plugins/intro

The existing CDN version is pinned to 4.2.3 because the application currently uses its v4 API. Do not load v5 plugins into this page without a planned v5 migration and compatibility tests.

## Pre-production checklist

- [ ] Set all secrets in the server environment.
- [ ] Configure HTTPS reverse proxy, domain, callback routing and HSTS.
- [ ] Configure SMS adapter and test delivery/rate limits.
- [ ] Configure Google OAuth and test new/existing linked accounts.
- [ ] Run ZarinPal sandbox end-to-end tests including duplicate callbacks and wallet rollback.
- [ ] Back up and restore `site_accounts.sqlite` in staging.
- [ ] Verify unauthenticated requests to every disabled raw endpoint receive HTTP 410.
- [ ] Verify public JSON responses do not include holder names, IDs, share counts, raw percentage sums or company-history rows.
- [ ] Verify Premium group customization is rejected for free members and works only for the owner's account.
- [ ] Verify Super Admin bootstrap is one-time and first admin cannot demote the final Super Admin.
- [ ] Complete a manual responsive-browser pass on desktop and mobile.

This branch is not production-deployed by this document. Do not merge to production until the external provider credentials and staging checks are complete.
