# Tloomberg account, billing, support and product analytics architecture

## 1. Two independent data domains

The existing market-data database and the SaaS account database remain independent.

- **Market database:** instruments, prices, shareholders, ownership snapshots and TSETMC-derived information.
- **Identity PostgreSQL database:** accounts, credentials, sessions, role and permission facts, MFA and legal acceptance.
- **Commerce PostgreSQL database:** plans, subscription orders and entitlements, payment attempts/events/refunds, wallet ledger, bank-account verification state, withdrawals and referral rewards.
- **SaaS PostgreSQL database:** non-secret user projections, preferences, support tickets, CRM, consent, product analytics and staff audit data.

There are no cross-database foreign keys or shared tables. The browser receives neither database credential. The SaaS API authenticates visitors, checks customer entitlements and may call market-data services for authorized responses.

## 2. User identity and profile

Each account has an internal UUID primary key, a public random ID and an optional unique username. Verified identities are linked using the auth identities table:

- Public ID or username sign-in.
- Google OAuth identity, with verified email.
- Phone sign-in using OTP.
- Optional separately verified email and identity linking.

Linking a new identity requires re-authentication of that identity. Purchasing a subscription always requires a verified phone number, including users who originally signed in through Google. A phone typed into checkout is not verified until an OTP challenge completes.

The browser receives only an opaque HttpOnly, Secure, SameSite session cookie. Identity hashes session tokens at rest and is the source of truth for roles/permissions. SaaS and Commerce independently validate the opaque session through Identity's private introspection endpoint; commercial entitlements are read from Commerce. No role, payment state or entitlement is accepted from a browser cookie or user-supplied field.

## 3. Roles and customer plans are separate concepts

Roles represent staff authority; subscriptions represent customer feature access.

- **superadmin:** owner; can create and revoke administrative roles.
- **admin:** staff operations granted by explicit permissions.
- **support:** support tickets only.
- **data_manager:** market-data administration only.
- **user:** ordinary customer.

The data_manager role is distinct from paid plans. Paying for a subscription never grants permission to manage market data. The superadmin grants staff roles on the server. Every protected endpoint, report and export enforces permissions server-side; hiding a menu item is not authorization.

## 4. Subscription terms and feature access

The two paid plans are:
- **ویژه یک‌ماهه:** 899,000 Toman for 30 days.
- **ویژه سه‌ماهه:** 2,499,000 Toman for 90 days.

Both unlock all customer-facing features: shareholder graphs, detailed shareholder analysis, advanced screener, portfolio reconstruction, investor history and allowed exports. Neither grants market-data administration.

The free plan currently receives only the basic market overview and basic stock page. It does not receive chart entitlements. More free-plan restrictions can be added once product rules are finalized.

The frontend may blur premium charts and show a purchase action, but the market API must also check effective_user_features before returning chart data. Never download restricted chart data and rely only on CSS blur.

## 5. Payment and phone verification

Checkout asks for the buyer's phone number and completes phone verification before creating an order. The API creates an idempotent order, starts a provider payment attempt, and activates a subscription only after a cryptographically verified provider callback and reconciliation.

Both snappay and digipay are represented in the database. Live payments are not active until merchant credentials, current provider documentation, signed callback handling, sandbox testing and reconciliation are completed. A browser redirect alone must never activate a subscription.

## 6. Wallet accounting and withdrawal invariant

All money is stored as integer Toman in an append-only ledger with two buckets:

- **cash:** money the user personally deposited; potentially withdrawable.
- **gift:** referral or promotional credit; never withdrawable.

Displayed total = cash + gift. Available withdrawal = cash minus pending/approved withdrawal reservations. Eligible purchases spend gift first, then cash. Balance checks, reservations and ledger entries must be atomic within a database transaction and protected by row locks and unique idempotency keys.

Example: 400,000 Toman personal deposit + 800,000 Toman referral gifts = 1,200,000 Toman total wallet balance, but at most 400,000 Toman is withdrawable before other cash debits/reservations. The 800,000 Toman gift is not withdrawable.

Never maintain a manually editable balance as the source of truth. Correct mistakes using compensating ledger entries; do not update or delete ledger history.

## 7. Referral reward

Every inviter has one referral code. A referral invitation stores the phone number being invited. Merely typing or adding an unverified number must not mint money. Qualification requires that the phone is new to the platform, creates an account, verifies its OTP and is not linked to a pre-existing account.

After qualification, create exactly one 100,000 Toman gift ledger credit, one referral_rewards record and one SMS notification in notification_outbox to the inviter's verified phone. Unique constraints and idempotency keys prevent duplicate rewards. The gift is non-withdrawable.

## 8. Support

All signed-in customers can create tickets, view their own ticket history and reply. Support staff can access tickets within their permissions. Internal notes are staff-only. FAQ articles are stored and published independently. Private ticket bodies are never copied into product analytics. Payment, subscription, wallet and referral persistence is owned by the separate Commerce service.

## 9. Product analytics: eligible clicks, scroll and dwell intervals

The event model is designed to explain which components users activate, how far they scroll and how long they spend in each named section.

Event types:
- **click:** eligible clicks/taps, identified by stable component_id and element role.
- **scroll_depth:** milestones such as 25%, 50%, 75%, 90% and 100%.
- **section_view:** named sections entering the viewport.
- **active_time:** visible, focused time spent on a page or section, excluding hidden tabs and idle periods.
- **page_view, navigation** and product-level actions.

Events can be associated with a user UUID after sign-in or pseudonymous anonymous/session UUIDs before sign-in. Store page path, named component, role, timestamp, viewport dimensions and allow-listed metadata only. Do not store query strings containing free text or any form value.

**Explicit opt-in is required before product analytics begins.** The privacy notice explains click, scroll and dwell measurement; users can later withdraw consent. Never capture keystrokes, passwords, OTP values, phone/email field values, bank details, payment credentials, private ticket messages or session replays containing form data. Analytics consent defaults to off.

Recommended starting retention for raw event-level data is 12 months, followed by deletion or irreversible aggregation. The final privacy notice must state the actual retention period. Restrict event-level access to authorized analytics administrators and record administrative access in audit logs.

The initial schema has analytics_events and authenticated analytics_consents. Migration 002 adds consent history and event indexes; migration 003 adds current anonymous consent and event-ID idempotency. The implemented \`saas_core.domain.product_analytics.ProductAnalyticsService\` provides consent-gated ingestion, event validation, component-level engagement aggregation, ordered funnels, weekly cohort retention, audited user-level access and configurable event retention. A production HTTP adapter, browser collector and consent UI are still required; no site-wide analytics events are collected by the current static Pages build.

## 10. Right-side account menu

The menu is a fixed-width drawer opening from the right:
1. **Profile:** public ID, username, verified phone/email, account type, subscription, wallet balances and ledger, referral code/invitations.
2. **Buy subscription:** monthly/quarterly plans with SnappPay or DigiPay; verified phone required.
3. **Support:** FAQ, new ticket and ticket history.
4. **Settings:** Jalali/Gregorian calendar, Rial/Toman currency, light/dark theme, privacy and analytics consent.
5. **Logout.**

A staff console domain service is provided in \`saas_core.domain.admin_console.AdminConsoleService\`. It covers user/role management, subscription/payment review, wallet/withdrawal operations, referrals, support queues, FAQ and audit. Every write is permission-gated and audited transactionally. This is not a deployed HTTP adapter or browser console yet. Ordinary users and paid customers do not receive that authority.

## 11. Suggested first-party API surface

- GET/PATCH /api/v1/account/me, /api/v1/account/profile and /api/v1/account/preferences
- POST /api/v1/auth/otp/request, POST /api/v1/auth/otp/verify, GET /api/v1/auth/me, POST /api/v1/auth/logout
- POST /api/v1/checkout/orders, POST /api/v1/payments/{provider}/callback
- GET /api/v1/wallet, GET /api/v1/wallet/ledger, POST /api/v1/wallet/topups, POST /api/v1/wallet/withdrawals
- POST /api/v1/referrals/invitations, GET /api/v1/referrals
- POST /api/v1/tickets, GET /api/v1/tickets, POST /api/v1/tickets/{id}/messages, GET /api/v1/faq
- POST /api/v1/analytics/consent, POST /api/v1/analytics/events
- /api/v1/admin/*, protected by permission checks and immutable audit records.

## 12. Implementation boundary

The right-side menu, chart lock and checkout are frontend previews. The repository currently has the separate SaaS schema and wallet rules, but it does not yet have the production SaaS API, SMS sender, Google OAuth, payment callbacks, support service or analytics ingestion endpoint. Therefore wallet actions, referrals, tickets and role changes are not simulated as successful. Production activation requires deploying the dedicated PostgreSQL database and API, then integrating and testing each external provider.


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


## Microservices extraction — Identity Service, Phase 1

The initial microservice boundary is now explicit: Identity owns authentication
and authorization facts in a separate PostgreSQL database. The SaaS API calls
Identity over its private service network for session introspection and
permission claims. SaaS stores only a same-UUID, non-secret user projection to
retain local payment, wallet, subscription, support and CRM foreign keys.
SaaS remains the owner of product plans, subscriptions and commercial
entitlements. See `docs/identity/PHASE_1_IDENTITY_SERVICE.md` for deployment,
data migration, internal HTTP contracts and the production cutover sequence.


## Microservices extraction — Subscription & Payment Service, Phase 2

Commerce is independently deployed with its own PostgreSQL database. It owns plans, orders, payment attempts/events, subscription activation, refund request records, wallet ledger, withdrawal reservations, bank-account verification state and referrals/rewards. Commerce authenticates each browser session through Identity's private introspection API; it does not read Identity tables. The SaaS API no longer mounts payment or wallet routers and obtains subscription history/paid entitlements through the private Commerce API. The Edge routes public plans, subscription, payment and wallet paths to Commerce, while private internal endpoints remain inaccessible from Caddy.

The current SnapPay/DigiPay adapters intentionally return `PaymentNotConfigured`; no official merchant integration pack or sandbox evidence was available. Bank verification, payout execution, live top-ups and refund execution also remain disabled. Migration of existing SaaS commercial rows is a separate dry-run-first script and has not been run against production. See `docs/commerce/PHASE_2_COMMERCE_SERVICE.md` for service contracts, migration/cutover and production gates.
