# Tloomberg SaaS Delivery Roadmap

## Phase 1 — Foundation
- Define isolated market, SaaS and analytics data boundaries.
- Add PostgreSQL schema and seed subscription plans.
- Define identity, RBAC, payments, wallet, referrals, support, consent and audit.
- Implement wallet policy.
- Keep existing market API/database untouched.

## Phase 2 — Authentication and authorization (in progress)
Implemented on feat/tloomberg-saas-auth-phase2: username/password with Argon2id,
phone OTP challenge persistence, encrypted SMS outbox, per-phone OTP request
throttling, revocable opaque sessions, and server-side role/entitlement policy.
Remaining before production: Google OAuth, verified identity linking, password
reset, IP/distributed rate limiting, CSRF/CORS hardening, superadmin bootstrap,
SMS worker, PostgreSQL integration tests and deployment security review.

## Phase 3 — Account and dashboard UX (API foundation started)
Implemented on feat/tloomberg-saas-account-phase3: profile read/update, allow-listed
preferences, active session listing/revocation, subscription history and
server-resolved feature access. Dashboard frontend and PostgreSQL integration
tests remain. The current session cannot be revoked through the other-session
route. No market-data database or service changes.

## Phase 4 — Subscriptions and payments (lifecycle foundation started)
Implemented on feat/tloomberg-saas-payments-phase4: server-priced idempotent
subscription orders, verified-phone purchase gate, provider-neutral checkout and
verification contracts, payment attempt tracking, atomic/idempotent settlement,
subscription renewal extension, and refund-request schema. SnapPay/DigiPay live
adapters remain disabled pending official merchant integration packs and sandbox
credentials. Refund execution, reconciliation, PostgreSQL concurrency tests and
provider end-to-end tests remain.

## Phase 5 — Wallet and referrals (foundation started)
Implemented on feat/tloomberg-saas-wallet-referrals-phase5: separate cash/gift
ledger balances, append-only idempotent ledger posting, gift-first spending policy,
cash-only withdrawal reservations, verified-bank-account gating, referral codes,
verified-phone referral attachment, and reward issuance from the trusted paid
subscription settlement path. Top-up checkout, bank onboarding/verification,
admin withdrawal review, payout integration and advanced abuse controls remain
blocked or deferred pending provider contracts and security work. No market-data
database or service changes.

## Phase 6 — Support (API foundation started)
Implemented on feat/tloomberg-saas-support-phase6: public published-FAQ reads,
customer ticket creation/list/detail/replies, staff queue and conversation access,
assignment to active support/admin users, status changes, a separate internal-note
endpoint, role-gated FAQ management and audit records. Attachments, notifications,
staff/customer UI, SLA and advanced anti-abuse controls remain. No market-data
database or service changes.

## Phase 7 — Product analytics (foundation started)
Implemented on feat/tloomberg-saas-product-analytics-phase7: versioned opt-in
consent, authenticated consent-gated event batches, server-derived user/session
identity, event deduplication, privacy-minimized page/click/scroll/active-time
fields, aggregate overview/pages/heatmap/sequential funnels/retention reports,
minimum-cohort suppression and separately permission-gated user-level event
history. A consent-gated browser SDK/banner is now wired into the Pages frontend
for route changes, UI clicks, scroll thresholds and active time. Live telemetry
remains disabled until a deployed API and real login/session cookie topology are
configured. Admin analytics dashboard UI and automatic retention scheduling
remain. No market-data database or service changes.

## Phase 8 — Superadmin console (API foundation started)
Implemented on feat/tloomberg-saas-superadmin-phase8: superadmin-only dashboard
and APIs for user management, support/admin role assignment, plans, subscriptions,
payment reconciliation notes, wallet inspection and audited ledger adjustments,
withdrawal review, referral visibility and audit logs. The account page now
supports phone OTP login; first-time verified phones create standard accounts,
and only a backend match with the protected bootstrap phone secret receives
the superadmin role. The browser cannot choose roles. A superadmin-only UI gate
is present. Actual bank payout and provider payment overrides are not allowed.
No market-data DB or service changes.

## Phase 9 — Production readiness (engineering foundation implemented; release gates open)
Phase 9A–D on feat/tloomberg-saas-production-readiness-phase9 now includes:
- Threat model, trust boundaries, explicit release gates and NOT READY status.
- Dependency vulnerability audit (pip-audit), high-severity/high-confidence Bandit, Dependabot weekly checks for Python, GitHub Actions and Docker images, and pinned Actions commit SHAs. Latest confirmed combined-branch run: Python suite 198 passed in 12.83s; dependency audit, security analysis, and PostgreSQL backup/restore drill passed.
- Production startup configuration checks, explicit host/CORS allow-lists, cookie CSRF origin checks, security headers, readiness endpoint, structured redacted request logs and bounded PostgreSQL connection pool.
- Argon2id dummy verification for unknown user logins, serialized concurrent OTP rate limits, exact payment-event and admin-wallet idempotency, append-only wallet/reconciliation/admin audit tables.
- Checksummed migration runner with advisory locking, per-migration transactions, drift detection, exact DB identity check, explicit rejection of unknown/non-empty schema without migration ledger, and bounded grants to a pre-created runtime role.
- PostgreSQL 16 CI integration suite (198 Python tests on the verified combined analytics/auth/Phases 7–9 head) for authentication, RBAC and object isolation, standard-user denial across current staff/admin/analytics-admin GET routes, support internal-note confidentiality, consent analytics, payment settlement/replay, gateway fail-closed behavior, wallet concurrency/idempotency, OTP concurrency and immutable records.
- Age-encrypted custom-format PostgreSQL backup with SHA-256 and isolated decryption/restore. CI runs an ephemeral PostgreSQL 16 backup → encrypted archive → decryption → restore rehearsal using a separately provisioned read-only backup role; it asserts that role cannot mutate tables and verifies remote backup/restore TLS fail-closed behavior.
- Non-root API Docker image and hardened production Compose template with one-shot migration prerequisite, read-only filesystem, dropped capabilities, bounded process count, Caddy automatic HTTPS, public metadata-minimal readiness route, fixed proxy trust boundary, and external/unpublished PostgreSQL.
- A bounded, read-only HTTPS staging probe for /health and /ready with public-DNS checks and explicit non-production operator approval.

**Not production-complete.** PR #17 remains Draft and unmerged. Phases 1–8 remain separate unmerged Draft dependencies. Existing production DB baseline adoption, complete route-level RBAC/object-isolation matrix, official payment/SMS/bank sandbox tests, edge/IP/device/distributed rate limiting and WAF, independent penetration review, encrypted off-host backup retention/RPO/RTO and a real disaster-recovery drill, production monitoring/on-call and incident exercise, executed staging load/soak/failure tests, actual host/DNS/TLS deployment and rollback, and jurisdiction-specific legal/privacy review remain required before public production. No migration has been applied to a live/market DB; no production deployment or live-host load test was attempted.

## Phase 10 — Internal CRM foundation (backend delivered; staff UI deferred)

Implemented on the architecture stabilization branch:
- Dedicated CRM schema on SaaS PostgreSQL, keyed only by stable `users.id`.
- Customer directory/search, lifecycle stages, staff assignment, append-only interaction timeline and due-dated follow-up tasks.
- Separate `crm.read` / `crm.manage` permissions granted to admin and superadmin only. CRM routes require session-bound TOTP MFA. CRM staff MFA enrollment does not grant access to the Superadmin console.
- No raw phone/email replication, Market DB dependency, automated marketing, SMS/email campaigns or lead capture for non-registered prospects.
- Audit records for CRM reads and writes, append-only interaction history, exact role-permission and customer-isolation integration tests.
- Dedicated PostgreSQL tests and a cross-platform test that runs Market HTTP and CRM APIs side by side with independent databases.

Deferred: staff CRM browser workspace, task reminders, outbound messaging/provider campaigns, marketing consent/preferences, prospect/lead capture, exports, advanced segmentation and workflow automation. Phase 9 production deployment, legal/privacy and security gates remain applicable.


---

# Microservices extraction track

This track is separate from the historical SaaS product phases above. It
extracts one bounded business capability at a time from the existing SaaS
modular monolith; it does not imply that all product modules are already
microservices.

## Phase 1 — Identity Service (implementation on architecture branch)

- Added the independent `identity_service` FastAPI application and dedicated
  PostgreSQL schema for users, credentials, phone challenges, sessions, roles,
  permissions, TOTP MFA, legal-acceptance evidence, encrypted SMS outbox and
  audit.
- Added a private service-authenticated introspection contract. SaaS now
  validates browser sessions and reads permission claims from Identity whenever
  `IDENTITY_SERVICE_URL` is configured; service failures fail closed.
- Preserved current SaaS foreign-key integrity through a same-UUID, non-secret
  user projection. Password hashes are explicitly nulled in SaaS projections.
  Commerce owns subscriptions and feature entitlements after Microservices Phase 2.
- Delegated account profile edits, session listing/revocation, account
  suspension and staff-role changes to Identity. Administrative mutations are
  permission-gated inside Identity and require recent session-bound TOTP MFA.
- Added independent Identity migrations, a dry-run-first one-time SaaS-to-
  Identity copy tool, isolated database test workflow, non-root Docker image,
  production Compose services and Edge routing for `/api/v1/auth/*`.
- No existing production database or live environment was migrated or switched.
  Production cutover remains an operator-controlled step after a reviewed dry
  run, backup, test login and deployment of separate database credentials.

**Remaining hardening/release gates:** deploy separate managed Identity
PostgreSQL and secrets, run and review the one-time copy against a controlled
production snapshot, rehearse cutover and rollback in staging, configure
service-token rotation/secret-manager delivery and network restrictions, run
load/soak and independent security review. Google OAuth, password recovery,
identity linking and email-verification browser flows are not added in this
phase.


## Phase 2 — Subscription & Payment Service

Implemented on the architecture stabilization branch:
- Independent Commerce FastAPI application, PostgreSQL schema, migration runner,
  runtime database role, Docker image, Compose units and Edge routing.
- Commerce owns plan catalogue/prices, orders, payment attempts/events,
  subscriptions, cash/gift wallet ledger, withdrawal reservations and
  referral rewards. Identity remains the authority for sessions, user status
  and verified phone; SaaS consumes Commerce subscription history and
  entitlements through a private service-token contract.
- Idempotent server-priced orders snapshot both amount and duration; later
  catalogue changes cannot alter an existing purchase. Provider settlement
  is server-verified and atomically activates subscriptions. Provider adapters
  remain disabled until official merchant integration packs are supplied.
- Gift-first wallet checkout debits available gift then unreserved cash,
  records an immutable allocation, and activates the subscription in the same
  database transaction. Duplicate requests replay safely; conflicting keys,
  insufficient balances, pending withdrawals and in-flight gateway attempts
  are rejected.
- Independent migration checksums, CI database provisioning, Commerce service
  tests, backup/restore tooling and production configuration validation.

**Remaining production work:** merchant sandbox integration and settlement
reconciliation, wallet top-up flow, bank-account onboarding/verification,
admin withdrawal review and payout, refund execution, production managed
database and secrets, controlled source-data migration rehearsal, independent
backup retention/restore rehearsal, service-token rotation, load/soak tests
and reviewed production traffic cutover. No live payments, payouts, database
migration or production cutover are performed by this code change.


## Phase 3 — Market Data Service

Implemented on `refactor/architecture-stabilization-phase0`:
- Independent FastAPI Market Data application for instrument catalogue,
  symbol search, instrument reference detail and daily OHLCV.
- The source of truth remains the existing TSETMC holdings SQLite database.
  The service opens source data and the Site Sidecar read-only; the refresh
  worker is the only Sidecar writer.
- Readiness verifies the Sidecar layout version, completed refresh marker and
  source fingerprint. Stale Sidecar state fails closed with HTTP 503.
- Preserved current dashboard routes for symbol search and prices while adding
  canonical `/api/v1/market/instruments` contracts.
- Added a separate non-root Docker image, dependency manifest, production
  Compose service, Caddy routing and optional legacy-dispatcher HTTP adapter.
- Ownership, shareholder resolution, portfolio, screener and legacy site-state
  endpoints remain on Market API; Phase 3 does not claim to extract those
  bounded domains.
- Tests cover data correctness, read-only storage, API compatibility, routing,
  deployment isolation and a three-service Identity + Commerce + Market Data
  integration smoke test.

**Production cutover remains operator-controlled:** publish an immutable
Market Data image, deploy with existing market source/Sidecar mounts, verify
`/readyz` and representative symbol/price parity, then allow Caddy to route
the selected read paths. No live source database mutation or traffic cutover
is performed by this code change.


## Phase 4 — Ownership Service

Implemented on `refactor/architecture-stabilization-phase0`:
- Independent FastAPI Ownership process and non-root Docker image.
- Preserved four read contracts for symbol shareholder snapshots, identity-resolved
  ownership/change series, cross-company shareholder search and holder observation
  history.
- Reused the established Ownership domain logic and pipeline identity evidence;
  no duplicate identity algorithm or dashboard response-contract rewrite.
- Read-only source and Sidecar SQLite connections. Sidecar rows remain owned by
  the refresh worker; the API mounts its directory only for SQLite WAL
  coordination and uses `mode=ro` plus `query_only=ON`.
- Readiness checks enforce the completed refresh, current Sidecar version,
  ownership resolver schema and required source/projection tables.
- Added Caddy routing, production Compose service, optional compatibility
  dispatcher proxy and focused API/deployment/security tests.

No production Sidecar rebuild, source-data change or traffic cutover was
performed. An immutable image publish, representative parity check and
operator-controlled rollout remain before production activation.
