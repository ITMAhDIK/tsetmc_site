# SaaS operations runbook

## Scope and non-negotiable boundary

This runbook applies only to the dedicated Tloomberg SaaS PostgreSQL database and the separately hosted SaaS API. Do not point any command in this runbook at a TSETMC, market-data, collector, or analytics-source database. Do not copy database dumps, credentials, environment files, OTP payloads, payment secrets or bank-vault tokens into the Git repository.

The backup/restore scripts are operational helpers, not a managed backup service. They have only been syntax-checked in CI; a PostgreSQL restore rehearsal is still mandatory.

## Required backup environment

Use a dedicated PostgreSQL role with only the privileges needed to read the SaaS database. Configure secrets through a secret manager or protected shell environment; do not put credentials in shell history or commit them.

Required variables for backup:

- SAAS_DATABASE_URL: connection string for the dedicated SaaS database.
- SAAS_EXPECTED_DB_NAME: exact value expected from SELECT current_database().
- SAAS_BACKUP_DIR: private backup directory outside the Git checkout.

The scripts also require PostgreSQL client tools (psql, pg_dump, pg_restore) and sha256sum.

Do not embed the database password in a connection string passed to command-line tools. Prefer a protected .pgpass file (mode 0600) or a PostgreSQL service definition and review host-level process visibility.

## Create a backup

From a trusted host with network access to the SaaS database:

1. Create a directory on encrypted storage with access restricted to the backup operator.
2. Set the three variables above in the protected environment.
3. Run: bash scripts/saas_db_backup.sh
4. Store the .dump and adjacent .dump.sha256 files together. Transfer both to encrypted, access-controlled off-host storage.
5. Record the backup timestamp and result in the operations log, without recording credentials or connection strings.

The script checks the connected database name, rejects a backup directory inside the current Git repository, creates a PostgreSQL custom-format dump, checks that pg_restore can read its archive directory, writes a SHA-256 sidecar and applies owner-only file permissions. It does not encrypt the dump itself; storage and transfer encryption remain mandatory.

Choose and document backup cadence, retention, immutability and RPO/RTO before production. No scheduler, retention deletion job or remote storage integration is included here.

## Restore verification (non-production only)

Use a pre-created, empty, isolated PostgreSQL database on a non-production instance. Never run verification against the production database.

Required variables:

- SAAS_BACKUP_FILE: path to the .dump file.
- SAAS_RESTORE_VERIFY_DATABASE_URL: connection string for the isolated verification database.
- SAAS_RESTORE_VERIFY_EXPECTED_DB_NAME: exact expected verification database name.
- SAAS_PRODUCTION_DB_NAME: configured production database name, used by a second safety check.

Run: bash scripts/saas_db_restore_verify.sh

The script verifies the SHA-256 sidecar and archive readability, confirms that the target database name matches the expected verification name and differs from the configured production name, refuses a target containing user-schema tables/views/sequences, and restores in a single transaction without clean or database-drop operations. It then checks for core SaaS tables. The script cannot establish whether a different database name is hosted on the production cluster, so the operator must independently verify the target host/cluster is non-production before running it.

A successful command is only a structural restore check. The operator must additionally run migrations/business invariants, compare expected row counts, sample critical records, check wallet/payment reconciliation and record the measured recovery time. A production restore must use a separately approved disaster-recovery procedure, never this verification script.

## Deployment and migration controls

- GitHub Pages hosts static frontend assets only; deploy the FastAPI service to a Python-capable host with HTTPS and network isolation.
- Keep the SaaS database in a separate instance/database and use a least-privilege application role. Do not grant the web process superuser or schema-owner privileges.
- Apply migrations as a reviewed, separately authorized operation. Record the migration identifier, checksum, operator, start/end time and result.
- Back up and verify recovery before schema changes with destructive or data-rewrite behavior.
- Do not deploy a feature branch or a Draft PR. Promote reviewed commits through staging, run smoke/security checks, then use an approved production change window.
- The API must remain private until authentication, authorization, CSRF/CORS, rate limiting, HTTPS, secret and monitoring gates are approved.

## Monitoring and alerting requirements

The following are required before public traffic; no monitoring vendor or alert destination has been selected yet:

- API availability, request latency, HTTP 5xx rate and database connection failures.
- Authentication failures and OTP request/verification anomalies, aggregated without logging secrets or full phone numbers.
- Authorization denials and privileged admin actions, with audit-log review.
- Payment orders stuck pending, provider verification failures and gateway-event reconciliation backlog.
- Wallet ledger balance/reconciliation anomalies and withdrawal queues.
- Notification outbox age/retries/failures.
- Database storage, connection saturation, slow queries, backup freshness and restore-test age.
- Analytics retention-purge success/failure and support queue age.

Use redacted structured logs with request correlation IDs. Never log passwords, raw session tokens, OTPs, secret values, raw bank tokens, full payment credentials or private ticket content. Define alert thresholds from measured staging behavior rather than guessing them.

## Incident response

1. **Detect and preserve:** record UTC detection time, affected service/environment, alert evidence and current deployment identifier. Preserve relevant logs and audit records without copying secrets or unnecessary personal data.
2. **Contain:** restrict or disable the affected API/route/provider credential; revoke affected sessions; suspend compromised staff accounts; isolate the host/database when compromise is suspected. Do not delete evidence.
3. **Assess:** identify data, users, transactions, credentials and jurisdictions affected. Check admin audit logs, provider dashboards, wallet/payment reconciliations and backup integrity.
4. **Recover:** rotate exposed secrets, restore from a verified clean backup only under the approved recovery procedure, deploy a reviewed fix and verify critical invariants.
5. **Communicate:** notify internal owners and providers. Determine legal/regulatory/customer notification duties with qualified counsel for the applicable jurisdictions and actual incident facts.
6. **Learn:** preserve a timeline, root cause, impact, recovery time and corrective actions; update the threat model and tests.

Assign named incident commander, technical lead, communications owner and legal/privacy contact before launch. Run a tabletop exercise and record its outcomes.

## Legal/privacy review inputs

Before accepting real users, document the operating entity, user and provider locations, categories of personal/financial data, hosting and backup countries, subprocessors, cross-border transfers, retention/deletion, payment and consumer obligations, support/analytics consent language, incident-notification rules and the user's rights/contact channel. Obtain advice for the actual jurisdictions; this runbook is not legal advice and does not assert that any particular regime applies.
