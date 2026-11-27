# Phase 4 — Ownership Service microservice

Branch: `refactor/architecture-stabilization-phase0`

## Delivered

The existing `site_api.ownership_service.OwnershipService` is now served by
an independently deployable FastAPI application at
`ownership_service.api:app`. The service exposes the existing read contracts:

- `GET /api/v1/stocks/{symbol}/shareholders`
- `GET /api/v1/stocks/{symbol}/ownership-series`
- `GET /api/v1/shareholders/search`
- `GET /api/v1/shareholders/observation-history`
- `GET /healthz` and `GET /readyz`

Existing query parameters and response payloads are preserved, so the current
dashboard does not need a simultaneous contract migration. Caddy can route
these endpoints to the separate process; the legacy Market API handlers remain
available for deployments that have not enabled the new routing.

## Domain behavior retained

- Same-date ownership percentages use the number of issued shares on the
  observation date. Missing denominators remain unavailable.
- Identity grouping uses only the pipeline's verified/inferred source links and
  the established provable-majority-twin rule. Names remain a consistency
  check, not proof of identity.
- Ambiguous and unresolved holders remain visible and are not silently merged.
- Shareholder change events retain their current event classification and
  latest-valid-close-on-or-before-date valuation behavior.
- Cross-company search and observation history retain existing filtering,
  sorting and pagination behavior.

No duplicate identity resolver or second calculation implementation was added.

## Storage and process isolation

The Ownership process opens both the TSETMC source SQLite database and the
shared Sidecar using SQLite `mode=ro` and `PRAGMA query_only=ON`. It does not
run the refresh worker, create schemas, update Sidecar rows or write to the
collector database. The Sidecar directory is mounted with OS-level write
permissions only for SQLite WAL lock/shared-memory coordination; the database
connections themselves remain read-only. The refresh worker remains the sole
writer of Sidecar projection rows.

Readiness verifies the completed refresh marker, the current Sidecar layout,
the ownership resolver schema and the tables required by Ownership queries.
Incomplete or unavailable stores fail closed with HTTP 503. The service runs
as UID/GID 10001 to match the shared Sidecar volume and has no Identity,
Commerce, wallet or SaaS database connection.

## Deployment and verification

Added a dedicated non-root Docker image, dependency manifest, production
Compose service, Caddy path routing, environment example and an optional
legacy-dispatcher HTTP adapter. The four Ownership read paths route to
`ownership-api:8000`; instrument metadata and daily prices stay on Market
Data, and unrelated legacy routes stay on Market API.

Tests cover the standalone HTTP contract, shareholder snapshot and event
valuation parity, cross-company identity grouping, query parameter forwarding,
Persian URL encoding, read-only source and Sidecar connections, readiness,
fail-closed service outages, Caddy routing, Compose isolation and image user.
The prior Ownership regression suite remains in the full project test suite.

## Cutover status

The service and production routing configuration are implemented. No
production deployment, Sidecar rebuild, source-data change or live traffic
cutover has been executed. Publish an immutable Ownership image, verify
`/readyz` and representative response parity, then perform an
operator-controlled rollout.
