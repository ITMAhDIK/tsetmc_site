# Phase 7 — Product analytics

## Consent and collection
- Product analytics is opt-in per authenticated account; essential account operation is not disabled by refusal.
- Revoking consent blocks all subsequent ingestion. Existing events are retained for now; deletion/retention policy requires a separate product/legal decision.
- Event ingestion accepts bounded batches, server-binds the user identity, and uses a client event UUID for retry idempotency.
- Only allow-listed event names and metadata keys are accepted. Raw form values, passwords, OTPs, payment details, ticket content, query strings, fragments, email/phone values and arbitrary payloads are not accepted.
- Events include page view, click, navigation, scroll depth, active duration and explicit funnel steps. Session IDs are client-generated correlation IDs, scoped to the authenticated user in storage.

## Reporting and access
- analytics.read gates aggregate overview, event mix, page activity, funnel and weekly retention reports.
- analytics.users.read gates user-level activity and is granted only to superadmins by migration 005; each user-level report is audited.
- Reports are limited to date windows of at most 367 days.
- The current heatmap representation is component interaction intensity, not pointer-coordinate replay; no mouse coordinates or keystrokes are collected.

## API
- PUT /analytics/consent
- POST /analytics/events
- GET /analytics/admin/overview
- GET /analytics/admin/funnels
- GET /analytics/admin/retention
- GET /analytics/admin/users/{user_id}

## Boundaries and follow-up
- Migration 005_analytics.sql is re-runnable and only alters the isolated SaaS database.
- No market-data DB, collector, public market API or static GitHub Pages data is touched.
- Frontend consent UI and instrumentation are not yet wired. Data export/deletion, event retention/partitioning, anonymized visitor analytics, and legal/privacy review remain before production use.
