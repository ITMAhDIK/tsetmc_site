# Phase 7 — Product Analytics Service

Branch: `refactor/architecture-stabilization-phase0`

## Delivered microservice boundary

The Product Analytics capability is extracted from the SaaS modular monolith into
an independently deployable FastAPI service with its own PostgreSQL database.
Production Caddy routes `/api/v1/analytics/*` to Analytics; production SaaS no
longer mounts the legacy analytics router or requires Analytics tables for
readiness. Development retains a temporary compatibility router.

- API: `analytics_service.api:app`
- Dedicated schema and checksummed migration runner:
  `analytics_service/schema/001_analytics_core.sql`,
  `scripts/manage_analytics_migrations.py`
- Runtime pool is separate from the migration-owner connection.
- Identity is authoritative for user/session IDs, permissions and MFA. Analytics
  calls Identity's private `POST /internal/introspect` contract using a
  dedicated service token; it never queries Identity or SaaS tables.
- No cross-database joins or foreign keys. Identity UUIDs are preserved by the
  existing identity migration.
- Independent non-root Docker image, local/production Compose services,
  Caddy routing, health/readiness endpoints and a separate retention worker.

## Product behavior and reporting

- Consent defaults off. The server enforces the current consent policy version.
  A policy version change requires fresh opt-in.
- Authenticated, consented event batches support page views, navigation, clicks,
  scroll milestones, foreground-active time and feature views.
- Client event UUIDs make retries idempotent. The database serializes per-user
  ingestion and applies a cross-worker rate limit (600 events/minute by default).
- Reports include overview KPIs, popular pages, viewport click heatmaps,
  ordered event funnels, mature weekly D7/D30 retention cohorts and repeated
  contiguous 2–5-step page-view journeys.
- Aggregate groups below five distinct users are suppressed. User-level event
  and path inspection requires superadmin role, a dedicated permission and MFA
  verified within the last 15 minutes. Every individual read is audited.

## Privacy and abuse controls

- Request bodies are capped at 64 KiB before JSON parsing; batches are limited
  to 1–50 events.
- The event vocabulary and accepted fields are allow-listed. The API never
  accepts caller-supplied user/session identity, free-form metadata, raw URLs,
  query strings, fragments, form values, typed text, credentials, contact
  details, payment data, IP addresses or user-agent strings.
- Route IDs are normalized to `:id`; legacy paths are sanitized during import
  and arbitrary legacy metadata is discarded.
- Withdrawing consent immediately stops browser collection and deletes the
  user's identifiable raw events and ingestion-limit state. Append-only
  consent history remains as evidence.
- Raw event retention is bounded to 30–730 days (365 by default). The separate
  worker uses a PostgreSQL advisory lock and records each purge in the immutable
  audit log.
- Backup/restore scripts use a read-only backup role, age encryption, SHA-256
  sidecars, TLS hostname verification for remote PostgreSQL and isolated
  restore verification. An off-host script supports immutable rclone remotes.

## API surface

- `GET /analytics/consent`, `PUT /analytics/consent`
- `POST /analytics/events`
- `GET /analytics/admin/overview`, `pages`, `heatmap`, `funnel`,
  `retention`, `paths`
- `GET /analytics/admin/users/{user_id}/events`
- `GET /analytics/admin/users/{user_id}/paths`
- `DELETE /analytics/admin/retention/expired`
- `GET /health`, `GET /ready`

The public same-origin prefix is `/api/v1/analytics`; Caddy strips `/api/v1`
before proxying to the service.

## Safe legacy data migration

The dry-run-first importer reads the existing SaaS database with a read-only
source role and copies only consent, user/session-linked events and analytics
audit rows into the dedicated database in bounded batches. It preserves source
event IDs/order, normalizes paths, discards free-form metadata, and is
idempotent. It refuses same-database source/target configurations and
conflicting target rows. Source tables are never deleted or modified. The old
tables remain available for rollback until counts and reports are reconciled.

```bash
python scripts/manage_analytics_migrations.py --apply
python scripts/manage_analytics_migrations.py --check
python scripts/migrate_legacy_analytics.py --dry-run
python scripts/migrate_legacy_analytics.py --apply
```

## Browser SDK boundary

The existing consent banner and SDK use the versioned `/api/v1/analytics`
routes, send only allow-listed fields, record viewport-relative click
coordinates for heatmaps and expose explicit feature-view instrumentation.
If server-side consent withdrawal fails, local collection still stops and the
UI asks the user to retry. The public config remains deliberately empty:
tracking is **not enabled** until a same-origin HTTPS API, working login
session, privacy notice and legal approval are provisioned.

## Validation and release status

The Python workflow provisions a separate disposable Analytics PostgreSQL
database, applies checksum-protected migrations and runs unit plus PostgreSQL
integration tests. The security workflow builds the non-root image, checks
production Compose/Caddy routing, scans dependencies and code, and rehearses
encrypted backup/restore.

This code change does **not** deploy production infrastructure, provision
production secrets, migrate live user data, enable visitor tracking, change
DNS or switch live traffic. Admin analytics dashboard UI, external secret
rotation, off-host retention policy setup, high-volume load/soak tests and an
independent privacy/security review remain release gates.
