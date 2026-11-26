# Phase 6 — Analytics Service extraction

Branch: `refactor/architecture-stabilization-phase0`

## Delivered

Screener query construction, validation, Sidecar execution and export orchestration
now live in `site_api.analytics_service.AnalyticsService`:

- Allow-listed screener fields and comparison operators
- Parameterized numeric, text and date conditions
- AND/OR filter composition and safe sort-column/direction fallback
- Capped result limits for browser queries and XLSX exports
- Sidecar readiness checks for both query and export operations
- Public-field projection and the existing right-to-left XLSX builder

`SiteApi` retains `_screener_query`, `screener_query` and
`screener_export` as compatibility adapters. The Phase 0 API manifest and
Phase 1 dispatcher therefore keep the same routes, request body, JSON shape
and XLSX response type.

The pure public-row projection and XLSX builder are hosted by the analytics
module and re-exported from `server.py` under their historical helper names,
so existing imports and export behavior remain compatible.

## Safety and data boundary

The service reads only materialized Sidecar projections and does not mutate
source holdings or perform a refresh. Filter values remain SQL parameters;
only allow-listed field mappings, operators and sort expressions enter the
SQL string. Invalid conditions are ignored and unknown sort values fall back
to the default approved column/order.

This is an internal modular-monolith boundary, not a separately deployed
network microservice.

## Verification

`tests/test_analytics_service.py` uses a disposable Sidecar to check composition,
filter logic, input normalization, injection-resistant sort fallback, result
limits, XLSX output, Dispatcher routing and readiness enforcement. Existing
API-contract and XLSX regression tests remain part of the full repository suite.
