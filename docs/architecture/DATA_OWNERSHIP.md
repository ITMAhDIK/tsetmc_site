# Data Ownership and Persistence Boundaries

This is the baseline ownership map for the gradual TSETMC Site decomposition.
It distinguishes source-of-truth data from rebuildable projections and
user-owned state.

## Baseline data stores

| Store | Current owner | Contents | Write policy | Rebuildability |
|---|---|---|---|---|
| TSETMC holdings SQLite | Upstream collector/pipeline | Companies, trading days, shareholder holdings, corporate actions, data-gap and reconciliation records | Market Data, Ownership and legacy Market API open read-only with `mode=ro` and `query_only=ON` | Not rebuilt by the site |
| Site sidecar SQLite | Market Data projection pipeline (refresh worker) | Materialized instrument lookup, market summaries, ownership resolution projections and history ledger, plus legacy watchlist/filter state | Refresh worker is the only projection writer; Market Data and read APIs open it read-only | Mixed; see below |
| Static `docs/data` | Repository/deployment pipeline | Sector registry/category JSON and published sample workbooks | Git commit/deployment | Regenerated or replaced from approved source artifacts |
| Identity PostgreSQL | Identity Service | Canonical users, credentials, sessions, roles, permissions, OTP, MFA and legal evidence | Identity Service only | Durable business records; migrations/backups required |
| Commerce PostgreSQL | Commerce Service | Plans, orders, payment events, subscriptions, wallet ledger, withdrawals and referrals | Commerce Service only | Durable business records; migrations/backups required |
| SaaS PostgreSQL | SaaS core | Non-secret user projections, account preferences, support, CRM and product composition state | SaaS core only | Durable business records; migrations/backups required |
| Analytics PostgreSQL | Product Analytics Service | Versioned consent, append-only consent history, minimized user/session events, rate-limit state and immutable analytics audit | Analytics API and retention worker only | Durable consent/audit evidence; raw events expire by policy; migrations/backups required |

## Market Data Service boundary

The independently deployed Market Data API owns the public read contract for
instrument reference data and daily prices. It reads instrument names/market
lookup fields from the rebuildable `site_watch` projection and authoritative
CISIN/share-count/OHLCV fields from the source holdings SQLite database.
The source database is mounted read-only. The Sidecar volume is mounted
read-write only for SQLite WAL lock/shared-memory files; the Market Data
process opens the database using `mode=ro` and `PRAGMA query_only=ON`.
It shares the Market API's unprivileged file UID for WAL coordination, while
the refresh worker remains the only process that writes projection rows.

The Market Data API does not read shareholder identities, subscriptions,
wallet balances, Identity records or Commerce tables. The independent Ownership
API owns the HTTP read contract for shareholder snapshots, identity-resolved
ownership series, cross-company holder search and shareholder observation
history. It reads the source holdings database and refresh-worker-built Sidecar
projections through `mode=ro` / `query_only=ON`; the refresh worker remains the
only Sidecar projection writer. The Sidecar directory is mounted with WAL
coordination permissions only. Portfolio remains a separate capability, and
legacy stock-detail composition stays on the compatibility Market API.

## Sidecar classification

### Rebuildable projections

These tables are derived from the source market database by `refresh_summaries`:

- `site_watch`
- `site_company_daily_dedup`
- `site_excluded_holder_rows`
- `site_holder_latest`
- `site_holder_history`
- `site_shareholder_names`
- `site_investors`
- `site_symbol_holdings`
- `site_corp_action_counts`
- `site_meta` (refresh metadata, source fingerprint and projection version)

They are projections, not authoritative source records. A new service may own and rebuild them, but it must retain the source data provenance, sidecar layout version, source fingerprint and readiness marker. Do not expose partially rebuilt projections.

### Durable site state currently colocated with projections

- `watchlist`
- `saved_filters`

These are written by site endpoints and are not derived from the TSETMC database. The baseline schema has no `user_id` or tenant key for either table. They are therefore global site state today, not private per-user workspaces. Do not copy them into a new service as though they were already user-owned. A future migration must define ownership, default migration behavior and access isolation before enabling accounts.

## Non-negotiable data rules

1. The market source database remains read-only to all web/API services.
2. No SaaS identity, payment, wallet or support operation writes to the market database.
3. Derived caches/projections may be deleted and rebuilt only by their designated projection builder.
4. Durable state must not be stored in a disposable projection database.
5. Cross-domain joins must happen through explicit APIs or versioned read models, not cross-database foreign keys.
6. Every new table must declare an owner, source of truth, retention, rebuildability and migration path.
7. Public static artifacts must be intentionally selected and sanitized; never publish raw databases, secrets, raw account data or unreviewed exports.

## Target ownership (proposed)

| Capability | Target owner | Target persistence |
|---|---|---|
| Instrument reference data and daily prices | Market Data | Existing pipeline SQLite plus versioned, rebuildable Sidecar lookup; independent read-only API |
| Holder observations and identity resolution | Ownership | Pipeline DB and refresh-worker-built Sidecar projections; independent read-only API process |
| Portfolio reconstruction | Portfolio | Derived read model/cache; calculations from Ownership and Market contracts |
| Screener and saved filters | Analytics | Stateless query engine; saved filters move to user-scoped SaaS persistence |
| Watchlists | User Workspace / Account | User-scoped SaaS persistence |
| Identity, sessions, roles | Identity | Dedicated Identity PostgreSQL database |
| Plans, payments, wallet | Commerce | Dedicated Commerce PostgreSQL database with transactional ledger and verified provider callbacks |
| Tickets and FAQs | Support | SaaS PostgreSQL |
| Product events | Product Analytics | Consent-controlled analytics store with retention |

## Migration safeguards

- Use expand/contract migrations: add and backfill new storage, dual-read/compare when safe, cut over, retain a rollback window, and only then retire the old path.
- Never dual-write critical ledger state without idempotency and reconciliation.
- Keep source DB access read-only and bounded.
- Record row counts, checksums or deterministic aggregates before and after projection rebuilds.
- A projection is ready only after the expected version and completion marker are verified.
