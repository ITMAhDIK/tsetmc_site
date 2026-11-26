# Phase 7 — Product Analytics

Branch: `refactor/architecture-stabilization-phase0`

## Delivered domain boundary

`saas_core.domain.product_analytics.ProductAnalyticsService` adds consent-aware
analytics operations over the independent SaaS PostgreSQL database. It does not
connect to the market database and is not yet a separately hosted HTTP service.

- Current user and anonymous consent state, with append-only consent history.
- A strict event vocabulary: page view, navigation, click, scroll milestone,
  section view, active time and product action.
- Stable component IDs and element roles; no text labels or raw input values.
- Bounded event batches, UUID event IDs and idempotent retries.
- Consent checked server-side before any batch is inserted.
- Aggregated component-engagement heatmap, ordered event funnel and weekly
  cohort-retention reports.
- User-level event lookup protected by `analytics.read` and an
  `admin_audit_logs` record in the same transaction.
- Configurable raw-event retention and audited purge operation (365 days by
  default; production privacy notice must publish the actual policy).

## Privacy controls

- Consent defaults to absent/off. Stale consent versions are rejected.
- Authenticated and anonymous users have separate current-consent records.
- Caller-supplied event identity fields are rejected; trusted identity is
  supplied separately by the server-side session boundary.
- Query strings, fragments, free text, form values, credentials, phone/email,
  payment details, bank data and private ticket content are not accepted.
- Only event-specific metadata keys and short safe identifiers are accepted.
- Event timestamps, viewport measurements, scroll milestones, active durations
  and batch sizes are bounded.
- Event IDs are unique for safe retry; duplicates are ignored.
- Individual event access requires the analytics permission and is audited.

## Schema and usage

Apply schema migrations in order to the dedicated SaaS PostgreSQL database:

```bash
psql "$SAAS_DATABASE_URL" -v ON_ERROR_STOP=1 -f saas_core/schema/001_initial.sql
psql "$SAAS_DATABASE_URL" -v ON_ERROR_STOP=1 -f saas_core/schema/002_product_features_referrals_analytics.sql
psql "$SAAS_DATABASE_URL" -v ON_ERROR_STOP=1 -f saas_core/schema/003_product_analytics_ingestion.sql
```

A service instance requires a DB-API connection factory, an authoritative
server-side permission checker, and the currently published consent version.
The caller must derive `user_id` from a validated server session; anonymous and
session UUIDs must be first-party pseudonymous identifiers.

## Not claimed as production-complete

The repository still has no deployed SaaS HTTP API, consent UI integration,
browser event collector, scheduler, privacy notice publication, or hosting
secrets. The suggested `POST /api/v1/analytics/consent` and
`POST /api/v1/analytics/events` endpoints are contracts for a future adapter,
not live public endpoints. Do not enable tracking on the GitHub Pages site
until the consent notice, API host, secure session boundary and frontend
instrumentation have been provisioned and tested.
