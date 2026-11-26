# Cross-phase verification — Phases 0–7

Branch: `refactor/architecture-stabilization-phase0`

## Runtime boundaries

The repository has two independently deployable platforms. Phases 3–6 remain
domain services inside the Market application. Phases 7–8 are isolated SaaS
modules backed by a dedicated PostgreSQL database. The Market source SQLite
file is opened read-only; the derived Sidecar is a separate database. No
cross-database joins or foreign keys are used.

## Verification coverage

- **Phase 0:** versioned 27-route API manifest, unique method/path pairs, and
  compatibility with the legacy and extracted dispatchers.
- **Phase 1:** dispatcher and actual HTTP handler preserve query/body
  forwarding, URL decoding, JSON/XLSX response types and error behavior.
- **Phase 2:** external refresh worker, CLI wiring, singleton process lock,
  shared Sidecar lock and worker failure exit statuses.
- **Phase 3:** MarketService overview caching, market watch, symbol search,
  normalization, detail, prices and readiness.
- **Phase 4:** OwnershipService shareholder snapshots, source-ID resolution,
  ownership series, shareholder search and observation history.
- **Phase 5:** PortfolioService identity closure, holdings, history, pagination
  and as-of valuation.
- **Phase 6:** Screener allow-listed fields/operators, parameterized queries,
  Sidecar execution and safe XLSX export.
- **Phases 3–6 together:** disposable SiteApi composition test checks shared
  symbol/identity/price/valuation semantics in the same Sidecar.
- **Phase 7:** PostgreSQL-backed SaaS analytics tests cover consent, event
  ingestion/deduplication, safe metadata, funnels, paths, retention, audited
  individual reads and permission boundaries. Market and SaaS connections
  remain independent.
- **Public Market HTTP boundary:** socket-backed test exercises health/readiness,
  CORS allow/deny, public reads, write-route denial, legacy admin-route denial
  and SQLite `query_only`.

## Isolation

Tests use temporary SQLite source/Sidecar files and a disposable PostgreSQL
16 service. The PostgreSQL fixtures verify the expected test database name
before destructive cleanup. No tests call TSETMC or modify production data.

## Latest verified CI

Commit: `fb0a15fa6c627fa3fd075ccc56e31efdf50f4c34`

- Full Python and PostgreSQL integration suite: **373 passed, 0 skipped**.
- SaaS security workflow: all three jobs successful.
- The same commit was used for both successful workflows.

Run locally with `python -m pytest -q`. Green CI is not production traffic
evidence, an independent penetration test or proof of a live deployment.
