# Ownership Service

The Ownership Service is an independently deployable, read-only FastAPI
microservice for TSETMC shareholder ownership and disclosed holder changes.

## HTTP contract

| Method | Route | Purpose |
|---|---|---|
| GET | `/api/v1/stocks/{symbol}/shareholders` | Snapshot, resolved holders and same-date ownership percentages |
| GET | `/api/v1/stocks/{symbol}/ownership-series` | Company ownership series, major-holder shares and shareholder change events |
| GET | `/api/v1/shareholders/search` | Cross-company shareholder search and identity groups |
| GET | `/api/v1/shareholders/observation-history` | Per-holder observation history |
| GET | `/healthz` | Process health |
| GET | `/readyz` | Source and completed Sidecar readiness |

Existing request parameters and JSON response fields are preserved. Query
parameters are forwarded as multi-value lists to the existing
`site_api.ownership_service.OwnershipService` implementation. That preserves
the pipeline identity vocabulary and conservative identity resolution rules;
names are never used as identity proof, and unresolved groups remain explicit.

## Data and security boundary

The service reads the TSETMC source SQLite database and the shared Sidecar.
Both database connections use SQLite `mode=ro` and `PRAGMA query_only=ON`.
The Sidecar directory is mounted read-write at the operating-system level only
for SQLite WAL lock/shared-memory coordination; this process never writes
database rows or creates schemas. The refresh worker remains the sole writer
of Sidecar projections. No Identity, Commerce, wallet or SaaS database is
connected.

Readiness requires the current Sidecar version, the ownership resolver schema,
a completed refresh marker and the tables needed by Ownership queries. An
incomplete or unavailable source/projection returns HTTP 503. The service
exposes GET routes only; mutation methods are not implemented.

## Runtime

Required production variables:

- `APP_ENV=production`
- `OWNERSHIP_SOURCE_DB_PATH=/data/source/market.sqlite`
- `OWNERSHIP_SIDECAR_PATH=/var/lib/tloomberg/site_data.sqlite`
- `OWNERSHIP_TRUSTED_HOSTS=market-api.example.com,localhost,127.0.0.1`
- `OWNERSHIP_ALLOWED_ORIGINS=https://itmahdik.github.io`
- `OWNERSHIP_SQLITE_TIMEOUT_SECONDS=5`

Run locally with:

```bash
python -m pip install -r requirements-ownership.txt
uvicorn ownership_service.api:app --host 127.0.0.1 --port 8000
```

The production Docker image runs as UID/GID 10001 to match the existing
Sidecar volume owner. The source database is mounted read-only; the Sidecar
volume is shared with the refresh worker and the other read-only market APIs.
Caddy routes only the four Ownership paths above to this service. The old
Market API handlers remain as a compatibility fallback for deployments that
do not enable the new Caddy route.

## Migration status

This phase extracts the HTTP process boundary while reusing the already
separated Ownership domain implementation. No source schema, production data,
identity mapping, refresh algorithm or live traffic is changed. Caddy routing
is included in the deployment template, but production activation still
requires publishing the image and an operator-controlled deployment.
