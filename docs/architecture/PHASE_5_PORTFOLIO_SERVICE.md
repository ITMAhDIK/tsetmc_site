# Phase 5 — Portfolio Service microservice

Branch: `refactor/architecture-stabilization-phase0`

## Delivered

The established `site_api.portfolio_service.PortfolioService` is exposed by an
independently deployable FastAPI application at `portfolio_service.api:app`.
It preserves the existing GET contracts:

- `GET /api/v1/portfolios/investors?q&limit&sort_by`
- `GET /api/v1/portfolios/{id}?as_of_date`
- `GET /api/v1/portfolios/{id}/history?ins_code&as_of_date&limit&offset`
- `GET /healthz` and `GET /readyz`

A dedicated Portfolio HTTP client supports the existing dispatcher. When
`PORTFOLIO_SERVICE_URL` is configured, requests are sent to the independent
service and failures return 503 rather than silently falling back to the
in-process implementation. With no URL configured, legacy local-development
compatibility is preserved.

## Domain and valuation rules retained

- Investor identity remains the composite `(id_scheme, shareholder_id)`; the
  source data does not establish one universal exchange-wide investor ID.
- Identity closure uses the pipeline `raw_source_ids` links and provable-majority
  twins. Names alone never merge two identities.
- Same-day duplicate observations for linked IDs are collapsed only when
  quantities agree. Conflicting quantities are marked and are not valued.
- Multiple instrument codes for the same CISIN are treated as one economic
  security when calculating combined portfolio value.
- Holding value is shares multiplied by a positive, valid closing/reference
  price. Historical pricing never uses a future close; same-date issued-share
  totals are preferred for ownership percentages.
- Disclosure gaps remain unknown, not zero or an assumed sale. Changes in
  disclosed holdings are not represented as proof of exchange executions.
- The dated portfolio curve is reconstructed from available shareholder
  observations. It is not a complete transaction ledger and cannot establish
  holdings before the first available disclosure.

No second identity resolver or duplicate valuation implementation was added.

## Storage and process isolation

The Portfolio process opens the TSETMC source SQLite database and shared
Sidecar with read-only URIs and `PRAGMA query_only=ON`. It does not run the
refresh worker, create schemas, or write source or Sidecar rows. The refresh
worker remains the Sidecar projection writer. Readiness checks the completed
refresh marker and every source/projection table needed by investor lookup,
current positions and historical reconstruction.

The image runs as UID/GID 10001 to share SQLite WAL coordination permissions
with the existing market processes. Its root filesystem is read-only in
production, and it has no Identity, Commerce, wallet or SaaS database
connection. Only the small sector-reference JSON inputs are copied into
the image for sector-affinity output.

## Verification and cutover

Tests cover the independent HTTP contract, list/detail/history response parity,
read-only SQLite behavior, readiness, invalid identifiers, query forwarding,
Persian investor-ID paths, fail-closed service outages, Caddy routing, Compose
isolation and non-root image configuration. Existing portfolio history,
identity, event classification and valuation regression tests remain active.

No production image publish, Sidecar rebuild or live traffic cutover is
performed by this change. Publish an immutable Portfolio image, compare several
investor outputs against the legacy API, verify `/readyz`, then route live
traffic through an operator-controlled rollout.
