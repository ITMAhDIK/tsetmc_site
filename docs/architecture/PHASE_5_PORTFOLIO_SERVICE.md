# Phase 5 — Portfolio Service extraction

Branch: `refactor/architecture-stabilization-phase0`  
Status: internal application-service boundary; HTTP contract unchanged.

## Scope moved

Portfolio behavior now lives in `site_api.portfolio_service.PortfolioService`:

- Investor lookup from materialized investor/name projections
- Conservative identity closure using pipeline raw-source links and the shared
  majority-twin map
- Latest positions by symbol, as-of filtering and per-security deduplication
- Chunked chronological history streaming from `site_holder_history`
- Same-date linked-identity observation resolution and conflict handling
- Paginated historical portfolio observations
- Portfolio detail, confirmed/probable tiers, CISIN deduplication, historical
  valuation summary and sector-affinity calculations

The shared majority-twin map remains in `SiteApi` because OwnershipService also
uses it. The PortfolioService receives it as a dependency rather than duplicating
identity logic.

## Compatibility and data safety

`SiteApi` keeps compatibility methods for the existing portfolio entry points.
The versioned dispatcher, API paths, request parameters and response fields stay
unchanged. Portfolio calculations continue to use the same-date share denominator
and the latest valid closing price on or before the observation date. Missing
disclosures are not converted into zero balances, and identity/quantity conflicts
remain explicitly unavailable rather than being silently valued.

The source holdings database remains read-only. All portfolio projections and
chronological history are read from the existing source/Sidecar tables; this
phase does not add a new schema migration or run a production refresh.

This is still a modular-monolith extraction, not a separately deployed network
microservice.

## Verification

`tests/test_portfolio_service.py` uses disposable SQLite source and Sidecar
databases to validate investor listing, historical portfolio rows, valuation,
confirmed/probable tier semantics, detail/history summaries, input validation,
readiness checks and dispatcher routing. The full suite, including Phases 0–4,
must pass in CI.
