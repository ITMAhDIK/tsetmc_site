# Phase 7 — Product analytics

## Implemented foundation
- Explicit per-user product analytics consent, versioned by the server.
- Consent defaults to disabled. Revoking consent deletes that user's previously collected identifiable event rows.
- Event ingestion requires a valid authenticated session and active product-analytics consent.
- User and session identity are taken from the authenticated server session; clients cannot choose them.
- Batch ingestion accepts at most 50 events and deduplicates with a client event UUID.
- Supported events: page_view, click, navigation, scroll, active_time and feature_view.
- Only allow-listed, low-risk fields are stored. Query strings, fragments, full URLs and free-form text are rejected or ignored. Numeric/UUID path segments are normalized to :id.
- Aggregate reports include overview KPIs, top pages, click heatmap bins, sequential event funnels, and weekly D7/D30 retention cohorts (D7 and D30 use the day-7/day-30 activity windows).
- Aggregate overview is suppressed below five active users; small funnel steps and under-mature retention denominators are also suppressed.
- Aggregate reports suppress page/funnel/heatmap groups with fewer than five distinct users.
- Aggregate reporting requires analytics.read. User-level event history requires analytics.users.read, and retention cleanup requires analytics.retention.manage; both elevated permissions are granted only to superadmin.
- A retention purge helper is included; a trusted scheduler/operator must invoke it according to SAAS_ANALYTICS_RETENTION_DAYS (default 365 days).

## API routes
- GET /analytics/consent
- PUT /analytics/consent
- POST /analytics/events
- GET /analytics/admin/overview
- GET /analytics/admin/pages
- GET /analytics/admin/heatmap
- GET /analytics/admin/funnel
- GET /analytics/admin/retention
- GET /analytics/admin/users/{user_id}/events
- DELETE /analytics/admin/retention/expired (manual privileged purge)

## Data and privacy boundaries
- This foundation records authenticated, consented events only; anonymous tracking is intentionally not enabled.
- It does not collect IP addresses, user-agent strings, keystrokes, form values, passwords, raw click text, full URLs, or query parameters.
- Heatmap coordinates are normalized percentages of the viewport, not exact screen coordinates.
- User-level inspection is protected by a dedicated permission distinct from analytics.read.
- All reads and writes target the dedicated SaaS PostgreSQL database. No market database, market API, collector or GitHub Pages market frontend is involved.

## Not included
- Frontend consent banner/event SDK and admin analytics dashboard UI.
- Automatic retention scheduler/worker; only a bounded manual purge operation and helper are present.
- Cross-device anonymous identity stitching, marketing attribution and export APIs.
- PostgreSQL integration, high-volume ingestion/load tests, dashboard query benchmarks and privacy/security review have not been run.

## Validation status
Pure event/path validation tests are included. PostgreSQL integration, permission matrix, consent-revocation deletion and concurrency/deduplication tests remain outstanding. Keep this PR Draft and the API private until these validations are complete.
