# Tloomberg SaaS Architecture — Foundation v1

## Five isolated data domains
1. Market data: existing market pipeline/database, consumed through the
   existing read-only market service.
2. Identity: a dedicated FastAPI/PostgreSQL service owning credentials,
   password hashes, OTP challenges, sessions, MFA, roles, permissions and
   immutable legal-acceptance evidence.
3. SaaS core: dedicated PostgreSQL for same-UUID user projections, account
   preferences, support, CRM and product-facing API composition.
4. Commerce: independent FastAPI/PostgreSQL service owning plans, orders,
   payments, subscriptions, wallet ledger, withdrawals and referrals.
5. Product analytics: consent-controlled events with a strict data allow-list.

No direct database joins or cross-database foreign keys are permitted between
Market, Identity, SaaS and Commerce. Identity and SaaS/Commerce communicate
only through private HTTP contracts. SaaS reads subscription and entitlement
state from Commerce rather than a local commercial copy. The application
authorizes Market API calls using the authenticated Identity principal and
Commerce subscription entitlements.

## Roles versus entitlements
Roles: superadmin, admin, support, user. Subscription entitlements are separate:
- superadmin: all product areas, including data management.
- admin/support: only explicitly assigned permissions.
- premium: analytical features, excluding data management.
- free: public/free entitlements only; final matrix awaits product-owner input.
Every protected API/export must enforce access server-side. UI blur is not
security.

## Identity
Identity is a separate deployable service and the authoritative owner of the
immutable internal UUID, public ID/username, Argon2id password hashes, phone
OTP challenges, opaque session hashes, RBAC, TOTP MFA and legal-acceptance
evidence. The service has a dedicated PostgreSQL database and does not connect
to the Market or SaaS databases. Its public browser routes are reverse-proxied
by the same-origin Edge; private introspection and account-administration
routes require a service credential plus an independently validated user
session where applicable.

SaaS retains a same-UUID projection of non-secret user fields so its local
operational foreign keys remain intact. Password hashes and session tokens
are never projected. SaaS and Commerce ask Identity to introspect presented
browser sessions and use returned role/permission claims for authorization;
an Identity outage fails closed. Commerce owns subscriptions and paid feature
entitlements. The current migration path
preserves UUIDs, hashes, active sessions, MFA seeds, role assignments and
legal evidence. Existing local auth routes remain only as an explicit
development/rollback compatibility path when no Identity URL is configured.

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
Phase 10 implements the CRM backend foundation using stable SaaS user IDs and audited APIs. The staff browser workspace, automated outreach and advanced CRM workflows remain deferred.


## CRM — Phase 10 backend foundation

The internal CRM runs only on the SaaS PostgreSQL database and references
customers by the stable `users.id` UUID. It provides staff-only customer
search, lifecycle stage and assignment, an append-only interaction timeline,
and due-dated follow-up tasks. The `crm.read` and `crm.manage` permissions
are separate from support-ticket access and are granted to admin/superadmin
roles only. Every CRM route requires a recent session-bound TOTP verification.

CRM records do not duplicate phone/email addresses, Market data, private ticket
messages, payment credentials, bank information, OTPs or analytics event
payloads. CRM read/write activity is audited; interaction history cannot be
updated or deleted. CRM staff MFA enrollment does not confer
`superadmin.console.access`.

The staff CRM UI, reminders, prospect capture and outbound campaigns are
deferred. No marketing message is sent by the Phase 10 backend.


## Microservices extraction — Phase 1: Identity Service

The first independent business service is Identity. It owns registration,
login, phone verification, sessions, MFA and role/permission evaluation.
SaaS calls its private introspection contract for each authenticated request
and retains only a local non-secret user projection with the same UUID to
preserve local operational foreign keys. Admin account suspension and role
changes are sent to Identity and require server-side permission and recent
TOTP verification. Deployment, migration and API details are documented in
`docs/identity/PHASE_1_IDENTITY_SERVICE.md`.

This is a deliberate strangler migration: Identity can be deployed and its
database seeded before the Edge is switched. The copy utility defaults to
dry-run, refuses identical databases and non-empty user targets, and reads
the SaaS source in read-only mode. No live database has been migrated by this
code change. The SaaS application remains a modular monolith for payments,
wallet, support, analytics and CRM; those domains are not split by this phase.


## Commerce — Microservices Phase 2

Commerce is an independently deployed FastAPI service with a dedicated
PostgreSQL database. It is the sole owner of plan pricing, subscription
orders, payment attempts/events, active subscriptions, wallet cash/gift
balances, withdrawal reservations and referral rewards. Browser sessions are
validated by Identity; Commerce keeps only a same-UUID non-secret user
projection and does not query Identity tables. SaaS calls Commerce over a
private service-token contract for subscription history and feature
entitlements. The same-origin Edge routes the public plan, subscription,
payment and wallet API paths to Commerce; private internal routes are not
exposed through the Edge.

The wallet supports full subscription checkout with gift-first spending and
then unreserved cash. Ledger debits, an append-only allocation record, the
order-paid transition, subscription activation and first-purchase referral
reward commit atomically. Orders snapshot both server price and plan duration.
No cross-database joins or foreign keys are used. Provider checkout/refund,
bank-account verification, payout and production data cutover remain gated
until official provider contracts, credentials and operational approvals exist.
