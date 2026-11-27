# Phase 6 — Screener Service microservice

Branch: `refactor/architecture-stabilization-phase0`

## Delivered

The stock-screening domain has been extracted to
`screener_service.service.ScreenerService`. The legacy
`site_api.analytics_service.AnalyticsService` import remains as a compatibility
alias, so existing Market API callers and tests keep working.

The independently deployable FastAPI application
`screener_service.api:app` exposes:

- `POST /api/v1/screener/query` — validated filter query returning allow-listed
  JSON rows.
- `POST /api/v1/screener/export` — the same filter contract as an XLSX
  attachment.
- `GET /healthz` and `GET /readyz`.

The existing filter semantics are retained:

- `AND` / `OR` composition.
- Numeric, text, corporate-action and Gregorian-date fields from the existing
  explicit field allow-list.
- Comparison operators `>=`, `<=`, `=`, `>`, and `<`.
- Query result limit remains capped at 500 rows; XLSX export remains capped at
  5,000 rows.
- JSON and XLSX output contain only the approved public column allow-list;
  internal investor IDs and identity-resolution metadata are not exported.

The HTTP adapter accepts only the two screening POST paths, bounds request and
response sizes and timeouts, validates response content type, and fails closed
with 503 when a configured Screener service is unavailable.

## Data ownership and isolation

Screener reads only the materialized SQLite Sidecar projections:
`site_watch`, `site_holder_latest`, `site_shareholder_names`,
`site_corp_action_counts`, and the readiness metadata/history marker. It
opens the Sidecar with URI `mode=ro` and `PRAGMA query_only=ON`. It does not
mount or open the source holdings database and does not write to Sidecar.
The Market refresh worker remains the sole Sidecar projection writer.

The production container runs as UID/GID 10001 with a read-only root
filesystem, no dropped-back-in capabilities, bounded process count and an
explicit HTTPS CORS/Trusted Host allow-list. Caddy routes only the query and
export paths to `screener-api:8000`.

Saved-filter persistence deliberately remains on the Market API for now: the
existing global `saved_filters` table has no authenticated account owner.
Moving it into this service before defining ownership and authorization would
preserve an unsafe shared-state model. A per-account saved-filter feature
belongs with the account/preferences boundary, not the public screening engine.

## Verification and cutover

Tests cover the independent HTTP contracts, AND/OR and numeric/date filters,
invalid sort/filter inputs, XLSX integrity and column allow-list, read-only
Sidecar access, readiness failures, input AST/limit bounds, dispatcher
forwarding, content-type handling, fail-closed service outages, Caddy routing,
Compose isolation and non-root image configuration. Existing AnalyticsService
and Market API compatibility tests remain active.

No production image publish, Sidecar rebuild or live traffic cutover is
performed by this code change. Publish an immutable Screener image, verify
`/readyz`, compare representative query/export results against the legacy
Market API and then perform an operator-controlled rollout.
