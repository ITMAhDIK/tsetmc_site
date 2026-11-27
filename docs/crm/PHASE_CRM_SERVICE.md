# Phase — Independent CRM Service

## Ownership boundary

The CRM service owns customer lifecycle metadata, staff assignment, interaction history,
follow-up tasks and CRM audit events in its own PostgreSQL database. It does not own
credentials, sessions, roles, subscriptions, payments, wallet entries or Market data.

Customer identity is represented by the same stable UUID issued by Identity. CRM stores
a minimized customer projection (username, display name, account status, verification
booleans and creation timestamp); it never copies phone numbers, email addresses,
password hashes, session tokens, MFA secrets, payment details or ticket bodies.

Identity remains authoritative for authentication, roles, permissions and MFA. Every
CRM request introspects the caller's session through the private Identity API. CRM
requires the Identity-issued `crm.read` or `crm.manage` permission and recent
session-bound MFA. These permissions are granted to admin/superadmin, not the support
role by default.

## API

The SaaS HTTPS edge routes `/api/v1/crm/*` to the independent CRM API. The CRM app
serves `/crm/*` routes after the edge strips `/api/v1`.

- `GET /crm/customers` — bounded search and pagination.
- `GET /crm/customers/{customer_id}` — customer profile.
- `PATCH /crm/customers/{customer_id}/profile` — lifecycle stage and assignment.
- `GET|POST /crm/customers/{customer_id}/interactions` — interaction timeline and append.
- `GET|POST /crm/followups` — due-dated follow-up work.
- `PATCH /crm/followups/{followup_id}/status` — complete/cancel/reopen a follow-up.

Interaction history is append-only at the database level. Reads and writes produce
CRM audit records. The internal projection endpoint is not exposed by the public edge
and requires a separate high-entropy projection token.

## Migration

The schema is managed by checksum-protected migrations using a dedicated migration
connection and expected database name. Runtime credentials must be a pre-created,
least-privilege database role.

Legacy import is intentionally separate from schema migration:

1. Configure `CRM_IMPORT_SOURCE_DATABASE_URL` as a read-only role on the legacy SaaS DB.
2. Configure `CRM_IMPORT_SOURCE_DATABASE_NAME` and `CRM_MIGRATION_DATABASE_NAME`.
3. Run `python scripts/migrate_legacy_crm.py --dry-run` and reconcile the counts.
4. Back up both databases and run `python scripts/migrate_legacy_crm.py --apply` in a
   controlled maintenance window.
5. Compare counts and representative UUIDs before routing production traffic.

The importer preserves customer, interaction and follow-up UUIDs and never modifies
the source database. It is idempotent for imported interaction and follow-up IDs.

## Deployment

Local Compose provides an isolated CRM PostgreSQL instance, migration job and API.
Production Compose runs the API as a non-root, read-only container on the private SaaS
network; the database is supplied as a separate external PostgreSQL service. Caddy
routes the public CRM prefix only to the CRM API.

## Verification and cutover gates

The CRM workflow applies and checks the PostgreSQL schema, runs unit/contract tests,
PostgreSQL constraint and authorization tests, and validates local/production Compose.
The normal full SaaS workflow remains responsible for cross-service regression.

No production CRM database is provisioned or migrated by CI. Before cutover, provision
the independent database and secrets, run the read-only import rehearsal, wire the
Identity customer-projection publisher to the private CRM projection endpoint, verify
projection freshness and data parity, then route the production edge. Do not remove
the legacy SaaS CRM tables until rollback and parity checks are accepted.
