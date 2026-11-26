# Phase 3 — Market Service extraction

Branch: `refactor/architecture-stabilization-phase0`

## Delivered

Read operations for the market dashboard and individual instruments were moved
from the legacy `SiteApi` implementation into
`site_api.market_service.MarketService`:

- Market overview and its five-minute response cache
- Market watch, filtering, sorting and pagination
- Symbol search
- Persian/Arabic symbol normalization and lookup
- Stock detail, including the identity-resolved ownership history projection
- Daily OHLCV prices sourced from `trading_days`

## Compatibility

`SiteApi` retains the old method names as compatibility adapters. The Phase 0
versioned API contract and Phase 1 dispatcher continue to invoke those methods.
HTTP routes, query parameter names, status codes and JSON fields are unchanged.

The service receives connection factories, Sidecar readiness validation,
metadata access and formatting/query helpers through constructor injection.
It does not import `server.py`, start an HTTP listener or write to the source
holdings database.

## Verification

`tests/test_market_service.py` uses disposable SQLite files to validate summary
payloads, market filters and pagination, symbol search and normalization, the
stock-detail history read model, daily OHLCV source selection and OHLC fallbacks.
Dispatcher integration cases verify that the Phase 1 route table reaches the
extracted Phase 3 service through the legacy compatibility adapter.

This is an internal modular-monolith service boundary, not an independently
deployed network microservice.
