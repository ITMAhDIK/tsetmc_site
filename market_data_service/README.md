# Market Data Service

Independent, read-only microservice for instrument reference data and daily OHLCV.

## Ownership

- The TSETMC holdings SQLite database remains the authoritative source for
  company identifiers and trading-day prices.
- The service reads the source database and Sidecar with SQLite `mode=ro` and
  `PRAGMA query_only=ON`. The source is mounted `:ro`; the Sidecar volume is
  writable only for SQLite WAL lock/shared-memory files. The service runs as
  the Market API's unprivileged file UID for WAL coordination.
- The existing Site Sidecar remains a rebuildable projection. Market Data
  reads only instrument lookup fields and refresh metadata from it. The service
  never writes to the Sidecar.
- The refresh worker remains the only writer of rebuildable Sidecar projections.
- This service does not connect to Identity, Commerce, SaaS PostgreSQL,
  Ownership, Portfolio or the collector's write path.

## API

- `GET /api/v1/market/instruments?q=&market=&limit=&offset=`
- `GET /api/v1/market/instruments/{symbol}`
- `GET /api/v1/stocks/search?q=&limit=` (dashboard compatibility)
- `GET /api/v1/stocks/{symbol}/prices?start_date=&end_date=&limit=`
  (dashboard compatibility)
- `GET /healthz`
- `GET /readyz`

All application routes are GET-only. A stale or incomplete Sidecar fails
readiness and data requests with HTTP 503 instead of serving stale mappings.
Prices are read from `trading_days` and ordered chronologically in the
response. Missing OHLC values use the existing TSETMC close-price fallback.

## Deployment

The production Market Compose stack runs a dedicated `market-data-api`
container. It mounts the source database read-only and opens the Sidecar in
query-only mode. The existing Market API keeps ownership, portfolio, screener
and legacy site-state routes.
Caddy directs instrument search/catalogue and daily price requests to the new
upstream while leaving unrelated endpoints on the existing API.

No source migration, schema mutation or data duplication is needed.
