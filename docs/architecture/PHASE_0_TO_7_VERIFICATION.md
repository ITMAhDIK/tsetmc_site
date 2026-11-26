# Cross-phase verification — Phases 0–7

Branch: `refactor/architecture-stabilization-phase0`

## Verification coverage

The complete pytest suite is the compatibility gate before Phase 8.

- **Phase 0:** the versioned 27-route API manifest is checked for unique
  method/path pairs and compatibility with both the legacy and extracted
  dispatchers.
- **Phase 1:** dispatcher and HTTP-handler tests preserve query/body forwarding,
  URL decoding, JSON/XLSX response types and 400/404/501 behavior.
- **Phase 2:** CLI wiring, external refresh worker, process singleton lock,
  shared Sidecar lock and worker failure exit statuses use disposable files.
- **Phase 3:** targeted MarketService tests exercise overview caching, market
  watch filtering/pagination, symbol search and normalization, detail, prices,
  dispatch behavior, and readiness after cache expiry.
- **Phases 3–6 together:** one disposable `SiteApi` composition root dispatches
  market, ownership, investor portfolio, screener query and XLSX export routes
  against the same materialized Sidecar. It asserts shared symbol/identity/
  price/valuation consistency and approved public output fields.
- **Phase 7 alongside market phases:** a second, independent DB-API connection
  is used for the SaaS analytics service while Phase 3–6 routes run on the
  market Sidecar. Consent, ingestion and component aggregation are exercised;
  SQL is checked to ensure analytics never queries market tables. Market API
  reads are repeated after analytics operations to detect cross-domain effects.
- **Phase 4–5:** shareholder identity, same-date holdings and as-of closing
  price valuation are asserted across service boundaries.
- **Phase 6:** screener output is checked against the same symbol/holder rows
  and XLSX contents are validated; private identity fields must not leak.
- **Phase 7 privacy unit tests:** consent-off rejection, consent version,
  event allow-list, safe metadata, idempotency, bounded event windows,
  permission checks, audited individual lookup and retention purge are tested.

## Isolation

All architecture integration fixtures use temporary SQLite files, disposable
Sidecars and mocked SaaS DB-API connections. Tests do not connect to the
production database, call TSETMC, change the Pages deployment or start a
production worker.

## Execution

Run the complete suite with:

```bash
python -m pytest -q
```

Phase 8 is gated on successful Push and Pull Request CI for the exact
pre-Phase-8 verification commit.
