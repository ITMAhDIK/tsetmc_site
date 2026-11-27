# Cross-phase verification — Phases 0–6

Branch: `refactor/architecture-stabilization-phase0`

## Verification coverage

The full pytest suite is the compatibility gate before Phase 7.

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
- **Phases 3–6 together:** one disposable `SiteApi` composition root is used
  to dispatch market overview/prices, ownership series, portfolio valuation,
  screener query and XLSX export against the same materialized Sidecar. This
  detects mismatched projections or import/adapter/dispatcher regressions
  between the services.
- **Phase 4–5:** shareholder identity, ownership percentages, same-date
  holdings and as-of price valuation are asserted across service boundaries.
- **Phase 6:** screener output is checked against the same symbol/holder rows
  and the XLSX package is validated; private identity fields must not leak.

## Isolation

All integration fixtures are temporary SQLite files and disposable Sidecars.
The tests do not connect to the production database, call TSETMC, change the
Pages deployment or start a production worker.

## Execution

Run the complete suite with:

```bash
python -m pytest -q
```

The Phase 7 implementation is gated on successful Push and Pull Request CI
for the exact pre-Phase-7 verification commit.
