# Data Ownership and Persistence Boundaries

This is the baseline ownership map for the gradual TSETMC Site decomposition.
It distinguishes source-of-truth data from rebuildable projections and
user-owned state.

## Baseline data stores

| Store | Current owner | Contents | Write policy | Rebuildability |
|---|---|---|---|---|
| TSETMC holdings SQLite | Upstream collector/pipeline | Companies, trading days, shareholder holdings, corporate actions, data-gap and reconciliation records | Site API opens read-only with `mode=ro` and `query_only=ON` | Not rebuilt by the site |
| Site sidecar SQLite | Site API process | Materialized summaries, ownership resolution projections, history ledger, plus legacy watchlist/filter state | API refresh writes projections; watchlist/filter endpoints write legacy state | Mixed; see below |
| Static `docs/data` | Repository/deployment pipeline | Sector registry/category JSON and published sample workbooks | Git commit/deployment | Regenerated or replaced from approved source artifacts |
| SaaS PostgreSQL | Not present in baseline `main`; defined on unmerged branches | Target users, sessions, roles, subscriptions, payment records, wallet ledger, referrals, support and consent | Future SaaS services only | Durable business records; migrations/backups required |

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
| Market reference data and prices | Market Data | Existing pipeline DB initially; read-only API |
| Holder observations and identity resolution | Ownership | Pipeline DB read-only + owned derived read model |
| Portfolio reconstruction | Portfolio | Derived read model/cache; calculations from Ownership and Market contracts |
| Screener and saved filters | Analytics | Stateless query engine; saved filters move to user-scoped SaaS persistence |
| Watchlists | User Workspace / Account | User-scoped SaaS persistence |
| Identity, sessions, roles | Identity | One canonical SaaS PostgreSQL authority |
| Plans, payments, wallet | Billing | SaaS PostgreSQL with transactional ledger and verified provider callbacks |
| Tickets and FAQs | Support | SaaS PostgreSQL |
| Product events | Product Analytics | Consent-controlled analytics store with retention |

## Migration safeguards

- Use expand/contract migrations: add and backfill new storage, dual-read/compare when safe, cut over, retain a rollback window, and only then retire the old path.
- Never dual-write critical ledger state without idempotency and reconciliation.
- Keep source DB access read-only and bounded.
- Record row counts, checksums or deterministic aggregates before and after projection rebuilds.
- A projection is ready only after the expected version and completion marker are verified.
