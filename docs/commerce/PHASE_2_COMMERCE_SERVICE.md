# Microservices extraction — Phase 2: Subscription & Payment Service

**Service:** Commerce  
**Branch:** `refactor/architecture-stabilization-phase0`  
**Status:** implementation slice complete; production cutover is not complete.

## Ownership boundary

Commerce is an independently deployable FastAPI application with its own PostgreSQL database. It owns the commercial lifecycle:

- Plans and server-side plan prices.
- Subscription orders, payment attempts, provider events and subscription activation.
- Refund request records and the immutable payment history.
- Cash/gift wallet accounts, append-only ledger, withdrawal reservations, bank-account verification state and referrals/rewards.

Identity remains the source of truth for user identity, account status and verified phone. Commerce validates each opaque browser session by calling Identity's private `/internal/introspect` contract using `X-Identity-Service-Token`. It does not read Identity tables. The user's stable UUID is copied into Commerce as a minimal projection so Commerce can enforce local foreign keys; phone and verification timestamps are refreshed from Identity on requests.

SaaS retains account preferences, support, analytics, CRM and staff UI/API composition. It no longer mounts the payment or wallet routers. Its account subscription history and feature authorization call Commerce with a distinct `X-Commerce-Service-Token`. The superadmin override still comes from Identity's role claims. If Commerce is configured but unavailable, subscription/feature reads fail closed instead of trusting the legacy SaaS subscription copy.

There are no cross-database foreign keys or direct joins. The Market database, collector, Sidecar and Market API are not accessed by Commerce.

## HTTP contracts

Public browser-facing routes are exposed through the existing same-origin Caddy edge:

| Route | Owner | Purpose |
|---|---|---|
| `GET /api/v1/plans` | Commerce | Active plan catalogue and server prices |
| `GET /api/v1/subscriptions` | Commerce | Current user's subscription history |
| `POST /api/v1/payments/orders` | Commerce | Idempotent order creation using a server-side plan price |
| `POST /api/v1/payments/orders/{id}/checkout` | Commerce | Begin an official hosted checkout adapter |
| `GET /api/v1/payments/orders/{id}` | Commerce | Read the caller's own order |
| `POST /api/v1/payments/providers/{provider}/return` | Commerce | Provider callback and server-side verification |
| `GET /api/v1/wallet` | Commerce | Cash, gift, total, reserved and withdrawable balances |
| `GET /api/v1/wallet/ledger` | Commerce | Caller-scoped ledger history |
| `POST /api/v1/wallet/withdrawals` | Commerce | Reserve user-funded cash against a verified bank account |
| `POST /api/v1/wallet/withdrawals/{id}/cancel` | Commerce | Cancel a still-requested withdrawal |
| `GET /api/v1/wallet/referrals` | Commerce | Referral code and aggregate counts |
| `POST /api/v1/wallet/referrals/attach` | Commerce | Attach a verified referred account |

Private service-only contracts:

- `GET /internal/users/{id}/subscriptions`
- `GET /internal/users/{id}/entitlements`

These require the dedicated Commerce service token and are not routed through the public Caddy edge. Identity introspection calls require the Identity service token.

## Financial invariants

- Amounts are integer Toman; the client cannot set the order amount or currency.
- Order creation requires an active account and an Identity-verified phone.
- Idempotency keys are bound to the same user and plan; reuse for different input is rejected.
- An order can have only one active checkout attempt.
- Subscription activation occurs only after a provider adapter returns a server-verified success, exact amount and provider event identity.
- Replayed provider events are accepted only when order, event type and payload digest all match; a conflicting replay is rejected.
- Payment settlement and subscription creation share one transaction; the unique payment-order link prevents duplicate subscriptions.
- Renewals begin at the later of now or the latest active subscription end, preserving the remaining paid time.
- Wallet balances are derived from append-only ledger rows. Cash and gift buckets stay distinct; gift is spent first and is never withdrawable.
- Pending/approved withdrawals reserve cash; withdrawal idempotency is enforced; only verified bank-account records are eligible.
- Referral rewards are gift credits created only from the first qualifying paid subscription settlement and protected by a unique reward/ledger key.
- Corrections to a ledger are compensating entries; updates/deletes are rejected by a database trigger.

## Deployment and migration files

- `commerce_service/`: API, Identity client, database pool, payment domain/gateway/repository and wallet/referral domain.
- `commerce_service/schema/001_commerce_core.sql`: independent Commerce schema and default plan catalogue.
- `scripts/manage_commerce_migrations.py`: separate checksum-verified migrations with advisory lock, database-name check, TLS verify-full for remote hosts and optional least-privilege runtime grants.
- `Dockerfile.commerce`: non-root image (UID/GID 10003).
- `docker-compose.saas.yml`: local Commerce database/API and dedicated port 58002.
- `docker-compose.saas.production.yml`: production Commerce migration/API units on the private backend network.
- `Caddyfile.saas`: routes only the commercial public API prefixes to Commerce.
- `scripts/migrate_saas_commerce.py`: source-read-only, dry-run-first migration of the existing SaaS-owned commercial history into an empty Commerce database.
- `scripts/commerce_db_backup.sh`, `scripts/commerce_db_restore_verify.sh` and `scripts/commerce_offsite_backup.sh`: separate age-encrypted Commerce backup, empty-target restore verification and immutable off-host upload workflows. The CI security workflow runs an isolated Commerce backup/restore drill with an independent ephemeral age key. It name-checks both databases, permits only the known empty target seed catalogue, preserves user/plan/order UUIDs, verifies row counts and wallet bucket totals, and never writes to the source.

The data migration script is not automatically run by Compose. Take source and target backups, pause relevant writes, run `--dry-run`, reconcile its inventory, and only then apply it in a maintenance window. Do not remove the legacy SaaS data until post-migration parity and application-level checks have been independently signed off.

## External integrations intentionally fail closed

No provider contract, merchant credentials or sandbox evidence was available during this phase. SnapPay and DigiPay adapters therefore remain explicit `PaymentNotConfigured` implementations. No gateway URL, signature scheme, callback field or refund payload has been invented. Checkout and callback verification will return 503 until official adapters are implemented and tested against merchant sandboxes.

Similarly, bank-account onboarding/verification is not wired to an approved provider. No public route can mark an account verified, so withdrawals reject unverified accounts. Admin withdrawal review/payout execution, wallet top-up checkout, refund execution/partial-refund policy, settlement reconciliation reports, configurable referral reward settings and distributed anti-abuse controls remain future work.

## Verification and production gates

The CI workflow creates a dedicated disposable PostgreSQL database named by `COMMERCE_TEST_DATABASE_NAME`, applies only Commerce migrations, checks their checksum ledger, and runs `tests/commerce`. The service tests cover Identity-backed session resolution, server-side plan pricing, order idempotency, verified-phone gating, CSRF origin checks for cookie writes, fail-closed Identity outage, private token authorization, subscription entitlements, caller-scoped wallet views and database-enforced ledger immutability. Pure tests cover payment amount validation, provider fail-closed behavior and cash/gift policy.

A successful CI suite is not production readiness. Before public traffic is switched:

1. Provision a dedicated managed Commerce PostgreSQL instance, separate runtime and migration roles, firewall rules and an independent scheduled encrypted/off-host backup policy. The repository now has separate Commerce backup/restore scripts and a disposable CI restore drill; production scheduling, retention, off-host object-lock policy and protected private identity still must be configured.
2. Rehearse the SaaS-to-Commerce data migration on a production-like snapshot, verify all row counts, financial totals, plan UUIDs, subscription statuses, outstanding withdrawals/refunds, and representative accounts. Keep a tested rollback/reverse-sync plan.
3. Publish protected Commerce and Identity service tokens; rotate them independently and restrict service-to-service network access.
4. Obtain official merchant integration packs, complete onboarding, implement the authentic server-to-server verify/refund contracts and run positive, negative, duplicate, amount-mismatch, timeout and reconciliation sandbox tests.
5. Implement and verify bank-account onboarding, admin review and payout execution before enabling withdrawals.
6. Verify production DNS/TLS, edge routing, browser CSRF/CORS behavior, monitoring, load/soak capacity and independent security review.

**No production database was migrated, no real payment or payout was attempted, no production secret was installed and no live traffic cutover was performed by this implementation.**
