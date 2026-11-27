# Phase 3 — Market Data Service

Branch: `refactor/architecture-stabilization-phase0`

## Delivered

Phase 3 turns the earlier internal `site_api.market_service.MarketService`
extraction into an independently deployable HTTP microservice:

- Added `market_data_service`, a dedicated FastAPI application with instrument
  catalogue/search, instrument detail and daily OHLCV routes.
- The existing dashboard contracts `GET /api/v1/stocks/search` and
  `GET /api/v1/stocks/{symbol}/prices` are preserved. A new canonical
  catalogue contract is available at `/api/v1/market/instruments` and
  `/api/v1/market/instruments/{symbol}`.
- Prices are sourced from the collector's `trading_days` table. Missing
  open/high/low values use the existing close-price fallback, and the API
  returns chronological rows with Gregorian and Jalali dates.
- Symbol search uses the existing refreshed `site_watch` projection but
  returns only instrument fields. Instrument detail exposes CISIN, current
  issued shares and latest daily close; it does not expose shareholder identity
  or portfolio records.
- A read-only repository opens both source database and Sidecar with SQLite
  `mode=ro` and `PRAGMA query_only=ON`. In production, the source database
  mount is `:ro`; the Sidecar volume is writable only for SQLite WAL
  lock/shared-memory files. The service uses the Market API's same unprivileged
  file UID for WAL coordination, but its SQL connection remains read-only.
  Readiness checks the Sidecar layout version, completed refresh marker and
  source-file fingerprint. Stale or incomplete projections return HTTP 503.
- Added a non-root `Dockerfile.market-data`, separate dependency manifest,
  production Compose service and Caddy route split. The new container mounts
  source SQLite read-only and uses a query-only Sidecar connection; the
  existing refresh worker remains the only projection-row writer.
- Added an optional HTTP adapter in the legacy dispatcher. When
  `MARKET_DATA_SERVICE_URL` is configured, compatible instrument/price routes
  proxy to the microservice and fail closed on outage. When unset, the legacy
  modular service remains available for local development and rollback.

## Ownership and compatibility

Market Data owns instrument reference data and daily price reads. The original
pipeline database remains the source of truth; no duplicate market database or
schema migration is introduced. The Sidecar remains a rebuildable projection,
not a durable data store. Market Data has no database connection, credentials
or foreign key to Identity, Commerce or SaaS.

The public Edge routes only instrument catalogue/search and daily price paths
to `market-data-api`. Ownership snapshots, shareholder identity resolution,
portfolios, screener logic and legacy site-state remain on the existing Market
API until their own bounded extraction phases. No route is moved merely because
its response happens to contain an instrument identifier.

## Verification

- Unit tests cover read-only SQLite access, source/Sidecar readiness and
  fingerprint mismatch, Persian/Arabic symbol normalization, catalogue
  filtering/pagination, instrument metadata, OHLCV source selection, date
  ranges and missing-OHLC fallbacks.
- HTTP tests verify canonical and legacy response contracts, not-found,
  invalid-query and read-only method behavior.
- Dispatcher tests verify optional service routing and prove that configured
  Market Data outages do not silently fall back to the in-process copy.
- Deployment tests verify Caddy route ownership, read-only source access, query-only Sidecar connections, non-root
  image configuration and the absence of Identity/Commerce database settings
  in the Market Data container.
- A three-service integration test exercises a real Identity session
  introspection request, Commerce user projection and paid entitlements, and
  Market Data instrument/price reads against three independent data stores.

No source market database was modified, no live production traffic was
switched, and no Identity or Commerce database was opened by Market Data.
