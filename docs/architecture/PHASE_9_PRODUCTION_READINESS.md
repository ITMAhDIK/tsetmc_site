# Phase 9 — Production readiness toolkit

## Delivered

- Threat model, security gates and incident/monitoring/backup runbooks.
- SaaS-only schema/connectivity healthcheck.
- Private custom-format PostgreSQL backup, atomic publication, SHA-256 manifest,
  archive verification and explicitly confirmed destructive restore.
- Bounded GET-only staging load probe.
- CI applies migrations 001–004 to ephemeral PostgreSQL 18, checks schema
  readiness and executes backup/verify/restore into a separate scratch database.

## Remaining production blockers

This phase does not deploy a SaaS API, choose a host, create production secrets,
configure external monitoring, schedule encrypted offsite backups, implement
admin MFA, wire browser consent, or publish legal terms/privacy notices. Phase 8
is a tested domain service, not an internet-facing admin API.

Static Pages authentication remains UI-only demo. Legacy market
`/api/v1/admin/*` routes are not SaaS staff APIs. Do not enable production auth,
payments, analytics or staff routes until secure sessions, MFA, CSRF/Origin,
verified provider callbacks, consent UI, retention policy and independent
security/legal review are complete.

The PostgreSQL CI restore test uses ephemeral infrastructure; it is not a
production backup/restore drill.
