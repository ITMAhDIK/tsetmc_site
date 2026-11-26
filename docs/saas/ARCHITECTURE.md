# Tloomberg SaaS Architecture — Foundation v1

## Three isolated data domains
1. Market data: existing market pipeline/database, consumed through the
   existing read-only market service.
2. SaaS core: dedicated PostgreSQL for users, auth identities, sessions, RBAC,
   plans, subscriptions, payment records, wallet ledger, referrals, tickets,
   preferences and admin audit.
3. Product analytics: consent-controlled events with a strict data allow-list.

No direct database joins or cross-database foreign keys are permitted between
market data and SaaS core. The application authorizes market API calls using
the authenticated SaaS identity.

## Roles versus entitlements
Roles: superadmin, admin, support, user. Subscription entitlements are separate:
- superadmin: all product areas, including data management.
- admin/support: only explicitly assigned permissions.
- premium: analytical features, excluding data management.
- free: public/free entitlements only; final matrix awaits product-owner input.
Every protected API/export must enforce access server-side. UI blur is not
security.

## Identity
A user has immutable internal UUID plus public ID/username. Password, Google
OAuth and phone OTP identities are linked through auth_identities. Linking
requires proof of control of both identities. Purchases require verified phone.
The active browser flows are Argon2id username/password and six-digit phone
OTP. Google OAuth and cross-provider identity linking are not currently exposed
as active browser flows. OTPs are HMAC-protected, short-lived, rate-limited and
single-use; signup OTPs are bound to the exact terms/privacy versions presented
when requested. Sessions store token hashes and can be revoked. The bootstrap
superadmin phone is configured out-of-band and receives the role only after
successful OTP verification. Privileged sessions additionally require TOTP
MFA with an independently encrypted seed, replay protection and a 12-hour
verification window.

## Payment lifecycle
Seed plans: monthly 30 days / 899,000 Toman; quarterly 90 days / 2,499,000
Toman. Provider-neutral lifecycle: create order -> initiate provider attempt ->
server-to-server verify -> settle/capture if required -> atomically record paid
event and activate subscription. Browser redirect is never proof of payment.
Provider event IDs and order keys are idempotent. SnapPay/DigiPay adapters must
follow the merchant's current official contract and sandbox tests before use.

## Wallet invariants
Integer Toman only. Append-only ledger split into cash (user-funded,
potentially withdrawable) and gift (promotional, non-withdrawable). Withdrawals
are limited to cash after pending/approved withdrawal reservations. Gift is
spent before cash for eligible purchases. Debits and reservations are atomic,
row-locked and idempotent. Corrections are compensating entries, not edits.

## Referrals
Unique referral code; a referred account can be attributed once. Reward is
unique per qualified referral and posted as a gift ledger entry in the same
transaction as qualification. SMS is an outbox side effect and retries must
not duplicate reward. Self-referral and suspicious velocity are blocked.
Initial inviter reward is 100,000 Toman; friend reward remains undecided.

## Support
Users can create tickets and see only their own tickets. Support agents use
permission-controlled queues. Internal notes are never visible to users.

## Analytics and privacy
Collect events only under recorded consent and retention policy. Record stable
component IDs, event names, page paths, timestamps, viewport, scroll depth and
active visible duration. Never capture typed text, raw input values, OTPs,
passwords, payment fields, bank details, private ticket messages or keystrokes.
Provide consent, retention, deletion and audited user-level analytics access.

## Deployment
The deployment template has two independent deployables: a read-only Market
API/Sidecar platform and a separate FastAPI/PostgreSQL SaaS platform. SaaS login,
admin and legal pages are served by a same-origin HTTPS Edge image so
HttpOnly/Secure/SameSite=Lax session cookies remain first-party. The Market
source SQLite is mounted read-only and public production routes are restricted
to read-only market and side-effect-free screener requests. Use independent
database credentials, protected secrets, encrypted backup/restore, migration
checksums, health/readiness, route-level Prometheus metrics and audit logs.
The Prometheus configuration is private; Alertmanager delivery, host monitoring
and on-call still require environment provisioning.
CRM is deferred and will integrate via stable user IDs and audited APIs.
