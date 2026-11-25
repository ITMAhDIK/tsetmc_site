# SaaS operations runbook

## Scope and non-negotiable boundary

This runbook applies only to the dedicated Tloomberg SaaS PostgreSQL database and the separately hosted SaaS API. Do not point any command in this runbook at a TSETMC, market-data, collector, or analytics-source database. Do not copy database dumps, credentials, environment files, OTP payloads, payment secrets or bank-vault tokens into the Git repository.

The Docker deployment is a production-oriented template, not a deployment to a real host. It deliberately does not create a database service or publish PostgreSQL. Supply a managed/dedicated PostgreSQL endpoint, TLS reverse proxy, secrets and an existing runtime database role before using it.

## Database roles and migrations

Use separate PostgreSQL identities:

- **Migration owner:** can create/alter the SaaS schema, migrations ledger and triggers. Its URL is supplied only to the one-shot migration service as `SAAS_MIGRATION_DATABASE_URL`.
- **Runtime application role:** a pre-created, non-owner LOGIN role with only SaaS application DML/sequence permissions. Its URL is supplied to the API as `SAAS_DATABASE_URL`.

Never run the web process as PostgreSQL superuser, database owner or migration owner. Create the runtime role out-of-band using your database administration/secret-management process; do not embed its password in this repository or in shell history.

### Apply and verify migrations

Run from a trusted deployment environment with the migration-owner connection available:

```bash
python scripts/manage_saas_migrations.py --apply --grant-role "$SAAS_APP_DATABASE_ROLE"
python scripts/manage_saas_migrations.py --check
```

The runner:
- requires `SAAS_MIGRATION_DATABASE_URL` and an exact `SAAS_EXPECTED_DATABASE_NAME` match;
- serializes migration runs with a PostgreSQL advisory lock;
- applies migrations in filename/version order, one transaction per migration;
- records SHA-256 checksums and application timestamps in `saas_schema_migrations`;
- refuses if an applied migration file has changed, if an unknown version exists, or if a non-empty schema has no migration ledger;
- grants bounded table DML and sequence access to the pre-created runtime role, but deliberately withholds access to the migration ledger;
- applies matching default privileges for future objects created by the migration owner.

**Existing database adoption is not automatic.** Databases that had migrations applied manually before this ledger existed will be rejected rather than guessed. Before production adoption, an operator must independently verify the exact schema state against all migration files and perform a reviewed, documented baseline-adoption procedure. Do not work around this by deleting schema objects or fabricating migration records.

### Migration rollback

Migrations are forward-only by default. A failed migration transaction rolls back that migration, but previously committed migrations remain applied. Do not edit an already-applied SQL migration to roll it back. Create a separately reviewed corrective migration or restore a verified backup in a controlled recovery window.

## Container build and production-oriented Compose

Build the API image from `Dockerfile.saas`. It:
- uses Python 3.12;
- runs as UID/GID 10001, not root;
- includes only the SaaS backend and migration runner;
- exposes the API on port 8000 and has a `/ready` health check.

The production Compose template is `docker-compose.saas.production.yml`. Configure all required variables through a protected environment or secret manager, not a committed env file. Required values include:

- `SAAS_MIGRATION_DATABASE_URL`: privileged migration-owner connection to the dedicated SaaS DB.
- `SAAS_DATABASE_URL`: separate low-privilege runtime role connection.
- `SAAS_EXPECTED_DATABASE_NAME`: exact database name used by both processes.
- `SAAS_APP_DATABASE_ROLE`: pre-created runtime role to receive grants.
- `SAAS_TRUSTED_HOSTS` and `SAAS_ALLOWED_ORIGINS`: explicit hostnames and HTTPS origins.
- `SAAS_OTP_HMAC_SECRET` and `SAAS_OUTBOX_FERNET_KEY`: independent, randomly generated secrets. Never reuse the DB password or each other.

Example invocation after provisioning the database/roles and secrets:

```bash
docker compose --env-file /secure/path/saas.production.env \
  -f docker-compose.saas.production.yml up --build -d
```

Compose runs the migration service first. The API service starts only after the migration container exits successfully. The API binds to `127.0.0.1` on the host; place a reviewed TLS reverse proxy in front of it. PostgreSQL is external and is never published by this stack. The app container uses a read-only root filesystem, tmpfs for temporary files, dropped Linux capabilities, no-new-privileges, bounded PIDs and bounded JSON logs.

The TLS proxy must set strict HTTPS/HSTS behavior and pass only trusted host/origin values. Do not enable Uvicorn forwarded-header trust for arbitrary client addresses. Configure trusted proxy networks explicitly at the infrastructure layer. The host firewall must not expose port 8000 publicly.

The deployment template does not configure a cloud load balancer, DNS, certificates, WAF, private network peering, secrets manager, backups, monitoring or autoscaling. Those are environment-specific release gates.

## Backup and restore

The repository includes `scripts/saas_db_backup.sh` and `scripts/saas_db_restore_verify.sh`.

The backup script requires:
- `SAAS_DATABASE_URL`: the dedicated SaaS database connection.
- `SAAS_EXPECTED_DB_NAME`: exact result expected from `SELECT current_database()`.
- `SAAS_BACKUP_DIR`: private directory outside the Git checkout.

The scripts require `psql`, `pg_dump`, `pg_restore` and `sha256sum`. Use a protected `.pgpass` or PostgreSQL service file rather than a password-bearing DSN. The backup script writes a custom-format archive and SHA-256 sidecar with owner-only permissions. It does not encrypt the archive itself.

Run backup from a trusted host and transfer both files to encrypted, access-controlled, off-host storage. Set cadence, retention, immutability and RPO/RTO based on the business recovery requirement. No remote backup-storage integration or retention deletion job is included.

For restore verification, use a pre-created, empty PostgreSQL database on a separately confirmed non-production cluster. Required variables:
- `SAAS_BACKUP_FILE`
- `SAAS_RESTORE_VERIFY_DATABASE_URL`
- `SAAS_RESTORE_VERIFY_EXPECTED_DB_NAME`
- `SAAS_PRODUCTION_DB_NAME`

The script checks the checksum and archive, rejects a database name equal to the configured production name, refuses a non-empty target schema, and restores in a single transaction without DROP/CLEAN. It then checks required core SaaS tables. The name check cannot prove that a different DB name is not on the production cluster: the operator must independently verify the target host/cluster. Never run the verification script against production.

### Verified CI rehearsal

The Phase 9 CI workflow now provisions a disposable PostgreSQL 16 service, applies all migrations, creates a custom-format backup, restores it into a separate empty database, and verifies the restored schema. The workflow uses PostgreSQL 16 client tools to match the service version. This validates the script path in a clean ephemeral environment; it is **not** evidence of encrypted off-host backups, production credentials, long-term retention or a real disaster-recovery drill.

After a real restore drill, also compare expected row counts, sample critical records, validate payment/wallet invariants and record measured recovery time.

## Monitoring and alerting requirements

No monitoring provider or alert destination has been selected. The following telemetry is required before public traffic:

- API availability, readiness, request latency, HTTP 5xx rate and database connection failures.
- Authentication failures and OTP request/verification anomalies, aggregated without full phone numbers or secrets.
- Authorization denials and privileged admin actions, with audit-log review.
- Pending/stuck payment orders, provider verification failures and gateway-event reconciliation backlog.
- Wallet ledger balance/reconciliation anomalies and withdrawal queues.
- Notification outbox age/retries/failures.
- Database storage, connection saturation, slow queries, backup freshness and restore-test age.
- Analytics retention-purge success/failure and support queue age.

Use redacted structured logs with request correlation IDs. Never log passwords, raw session tokens, OTPs, secret values, raw bank tokens, full payment credentials or private ticket content. Define alert thresholds from measured staging behavior rather than guessing them. Assign an on-call owner and rehearse delivery to the actual alert destination.

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
