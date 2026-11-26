# Phase 7 — Product analytics

## Implemented foundation
- Explicit per-user product analytics consent, versioned by the server.
- Consent defaults to disabled. Revoking consent deletes that user's previously collected identifiable event rows.
- Event ingestion requires a valid authenticated session and active product-analytics consent.
- User and session identity are taken from the authenticated server session; clients cannot choose them.
- Batch ingestion accepts at most 50 events and deduplicates with a client event UUID.
- Supported events: page_view, click, navigation, scroll, active_time and feature_view.
- Only allow-listed, low-risk fields are stored. Query strings, fragments, full URLs and free-form text are rejected or ignored. Numeric/UUID path segments are normalized to :id.
- Aggregate reports include overview KPIs, top pages, click heatmap bins, sequential event funnels with a 30-day step-conversion window, weekly D7/D30 retention cohorts based on first opted-in page view, and repeated contiguous page-view paths of 2–5 steps (the day-7 and day-30 windows are only evaluated after each window has fully matured).
- Repeated route patterns are derived within each authenticated session in timestamp order; sessions are never stitched together. Reports expose repetition count, distinct sessions/users and last occurrence.
- Aggregate repeated paths are suppressed below five distinct opted-in users. Per-user repeated paths require `analytics.users.read` and each read is written to the administrator audit log.
- Aggregate overview is suppressed below five active users; small funnel steps and under-mature retention denominators are also suppressed.
- Aggregate reports suppress page/funnel/heatmap groups with fewer than five distinct users.
- Aggregate reporting requires analytics.read. User-level event history requires analytics.users.read, and retention cleanup requires analytics.retention.manage; both elevated permissions are granted only to superadmin.
- A retention purge helper is included; a trusted scheduler/operator must invoke it according to SAAS_ANALYTICS_RETENTION_DAYS (default 365 days).
- Migrations `008_analytics_path_sequences.sql` and `009_analytics_event_order.sql` add partial covering indexes and a monotonic database event order. The event order keeps page sequences deterministic when multiple events in one batch share the same server timestamp; path sequences remain derived from the canonical event log rather than duplicated into a second table.

## API routes
- GET /analytics/consent
- PUT /analytics/consent
- POST /analytics/events
- GET /analytics/admin/overview
- GET /analytics/admin/pages
- GET /analytics/admin/heatmap
- GET /analytics/admin/funnel
- GET /analytics/admin/retention
- GET /analytics/admin/paths (repeated aggregate page paths)
- GET /analytics/admin/users/{user_id}/paths (audited individual route history; superadmin-only)
- GET /analytics/admin/users/{user_id}/events
- DELETE /analytics/admin/retention/expired (manual privileged purge)

## Data and privacy boundaries
- This foundation records authenticated, consented events only; anonymous tracking is intentionally not enabled.
- It does not collect IP addresses, user-agent strings, keystrokes, form values, passwords, raw click text, full URLs, or query parameters.
- Heatmap coordinates are normalized percentages of the viewport, not exact screen coordinates.
- User-level inspection is protected by a dedicated permission distinct from analytics.read.
- All reads and writes target the dedicated SaaS PostgreSQL database. No market database, market API, collector or GitHub Pages market frontend is involved.

## Not included
- Frontend consent banner and browser SDK are now wired into `docs/index.html`. The SDK records page views/navigation, allow-listed UI clicks, scroll thresholds and foreground-active time only after authenticated server-side opt-in. It is disabled by default until `docs/analytics-config.js` points to the real SaaS API and the website has working SaaS login/session integration.
- Admin analytics dashboard UI, automatic retention scheduler/worker, and production API/domain/session configuration remain outstanding.
- Automatic retention scheduler/worker; only a bounded manual purge operation and helper are present.
- Cross-device anonymous identity stitching, marketing attribution and export APIs.
- PostgreSQL integration, high-volume ingestion/load tests, dashboard query benchmarks and privacy/security review have not been run.

## Validation status
- Full combined Python suite: **198 passed in 12.83s** on PostgreSQL 16 CI.
- Frontend SDK: **10 Node.js tests passed**; `node --check` passed for both browser scripts.
- All ten SaaS migrations (001–010) applied with checksums and the migration lock; the migration history check passed.
- PostgreSQL route tests verify repeated two/three-step paths, exact repetition/session/user counts, same-timestamp ordering via `event_order`, no cross-session stitching, exclusion of non-page-view events, aggregate minimum-cohort suppression, user-level RBAC and audit logging.
- The standard-user route matrix also includes both new GET endpoints and confirms denial without analytics permissions.
- Dependency audit, Bandit analysis, encrypted backup/isolated restore, Docker/Compose/Caddy checks passed in the same CI revision.

The CI database is a disposable PostgreSQL 16 service. The frontend SDK and phone-OTP account UI are present, but live collection and real login remain disabled until a persistent SaaS API host, Kavenegar credentials/approved sender, protected bootstrap phone secret and same-site domain/cookie setup are provisioned. No live website visitor events or OTP SMS have been sent. Keep the API private until hosting, authentication, privacy and operational review are complete.
