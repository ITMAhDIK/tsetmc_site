# SaaS operations runbook

## Scope and non-negotiable boundary

This runbook applies only to the dedicated Tloomberg SaaS PostgreSQL database and the separately hosted SaaS API. Do not point any command in this runbook at a TSETMC, market-data, collector, or analytics-source database. Do not copy database dumps, credentials, environment files, OTP payloads, payment secrets or bank-vault tokens into the Git repository.

The Docker deployment is a production-oriented template, not a deployment to a real host. It deliberately does not create a database service or publish PostgreSQL. Supply a managed/dedicated PostgreSQL endpoint, TLS reverse proxy, secrets and an existing runtime database role before using it.

## Database roles and migrations

Use separate PostgreSQL identities:

- **Migration owner:** can create/alter the SaaS schema, migrations ledger and triggers. Its URL is supplied only to the one-shot migration service as `SAAS_MIGRATION_DATABASE_URL`.
- **Runtime application role:** a pre-created, non-owner LOGIN role with only SaaS application DML/sequence permissions. Its URL is supplied to the API as `SAAS_DATABASE_URL`.
- **Backup role:** a separate read-only role with only CONNECT, schema USAGE, table SELECT and sequence SELECT privileges. Its URL is supplied to the backup utility as `SAAS_BACKUP_DATABASE_URL`.

Never run the web process as PostgreSQL superuser, database owner or migration owner. Create the runtime role out-of-band using your database administration/secret-management process; do not embed its password in this repository or in shell history.

### Apply and verify migrations

Run from a trusted deployment environment with the migration-owner connection available:

```bash
python scripts/manage_saas_migrations.py --apply --grant-role "$SAAS_APP_DATABASE_ROLE"
python scripts/manage_saas_migrations.py --check
```

The runner:
- requires `SAAS_MIGRATION_DATABASE_URL` and an exact `SAAS_EXPECTED_DATABASE_NAME` match;
- requires remote database connection strings to explicitly use `sslmode=verify-full` (loopback-only local tests may omit TLS);
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

Build the API image from `Dockerfile.saas` in a controlled CI/release pipeline, scan it, and publish it to a private registry. Promote the exact image digest through staging and production; do not build mutable source independently on each production host. It:
- uses Python 3.12;
- runs as UID/GID 10001, not root;
- includes only the SaaS backend and migration runner;
- exposes the API on port 8000 and has a `/ready` health check.

The production Compose template is `docker-compose.saas.production.yml`. It requires `SAAS_API_IMAGE` and uses the exact same image reference for the migration and API services. Prefer an immutable registry reference such as `registry.example.com/tloomberg/saas-api@sha256:...`; do not use floating tags such as `latest` or `release-candidate` in production. Configure all required variables through a protected environment or secret manager, not a committed env file. Required values include:

- `SAAS_API_IMAGE`: approved API image reference, ideally pinned by digest.
- `SAAS_MIGRATION_DATABASE_URL`: privileged migration-owner connection to the dedicated SaaS DB.
- `SAAS_DATABASE_URL`: separate low-privilege runtime role connection.
- `SAAS_EXPECTED_DATABASE_NAME`: exact database name used by both processes.
- `SAAS_APP_DATABASE_ROLE`: pre-created runtime role to receive grants.
- `SAAS_TRUSTED_HOSTS` and `SAAS_ALLOWED_ORIGINS`: explicit hostnames and HTTPS origins.
- `SAAS_OTP_HMAC_SECRET` and `SAAS_OUTBOX_FERNET_KEY`: independent, randomly generated secrets. Never reuse the DB password or each other.

Example invocation after provisioning the database/roles and secrets:

```bash
# Build, scan and publish the release image in CI; copy its immutable digest
# into the protected environment file as SAAS_API_IMAGE.
docker compose --env-file /secure/path/saas.production.env \
  -f docker-compose.saas.production.yml pull
docker compose --env-file /secure/path/saas.production.env \
  -f docker-compose.saas.production.yml up -d
```


Compose runs the migration service first. The API service starts only after the migration container exits successfully. Caddy terminates TLS on host ports 80/443 and reverse-proxies to the API over the private Compose bridge. Set `SAAS_DOMAIN` to a public DNS name that resolves to the host; the Caddyfile uses automatic HTTPS. The read-only `/ready` endpoint is public but returns only a generic ready/not-ready status; it does not disclose the database name, schema details, secrets or customer data. The API has no published host port. PostgreSQL is external and is never published by this stack. The app container uses a read-only root filesystem, tmpfs for temporary files, dropped Linux capabilities, no-new-privileges, bounded PIDs and bounded JSON logs.

Caddy is assigned static bridge IP `172.31.240.10`; the API is `172.31.240.20`. Uvicorn trusts forwarded headers only from the exact Caddy address, so do not change that IP without updating both the network and Uvicorn allow-list. Confirm that `172.31.240.0/24` does not overlap any host, VPN, cloud VPC or container network. The host firewall should expose only 80/443 (and administrative access from trusted networks), never PostgreSQL or the API container port. The Caddy admin API remains container-local.

The deployment template does not configure a cloud load balancer, DNS, certificates, WAF, private network peering, secrets manager, backups, monitoring or autoscaling. Those are environment-specific release gates.

### Deployment and rollback procedure

1. Build one image from a reviewed commit and retain its commit SHA, SBOM and vulnerability-scan result.
2. Publish it to a private registry and record the immutable digest in the release record.
3. Deploy that same digest to staging. Run migration checks, API readiness, provider sandbox smoke tests, a bounded load probe and backup/restore checks.
4. Promote the same digest to production after the release gates are approved. Do not rebuild between staging and production.
5. Keep the previous known-good image digest and deployment environment record available for rollback.

Application rollback and schema rollback are different operations. Prefer rolling the application back to a backward-compatible prior image while keeping the schema forward-compatible. The migration runner intentionally has no automatic down-migrations. If a schema change is destructive or incompatible, stop the rollout and use a separately reviewed recovery plan; do not edit migration history or delete ledger entries to force a downgrade. A production database restore is a disaster-recovery action that requires explicit approval, a verified backup, a maintenance window and reconciliation of payments/wallet transactions that occurred after the backup.

## Backup and restore

The repository includes `scripts/saas_db_backup.sh` and `scripts/saas_db_restore_verify.sh`. Every archive produced by the backup script is age-encrypted by default; there is no plaintext backup mode in the normal backup workflow.

The backup script requires:
- `SAAS_BACKUP_DATABASE_URL`: a dedicated read-only backup-role connection to the SaaS database.
- `SAAS_EXPECTED_DB_NAME`: exact result expected from `SELECT current_database()`.
- `SAAS_BACKUP_DIR`: private directory outside the Git checkout.

The scripts require `psql`, `pg_dump`, `pg_restore`, `sha256sum` and `age`. Use a protected `.pgpass` or PostgreSQL service file rather than a password-bearing DSN. For non-loopback database hosts, include `sslmode=verify-full` in each runtime, migration, backup and restore connection string; provide the provider CA via `sslrootcert` when it is not in the system trust store. The backup script requires `SAAS_BACKUP_DATABASE_URL` for a dedicated read-only backup role and an `SAAS_BACKUP_AGE_RECIPIENT` public key, writes a custom-format `.dump.age` archive and SHA-256 sidecar with owner-only permissions, and never reads the private identity. Store the private `age` identity only in a secret manager or offline recovery vault.

Run backup from a trusted host and transfer both files to encrypted, access-controlled, off-host storage. Set cadence, retention, immutability and RPO/RTO based on the business recovery requirement. No remote backup-storage integration or retention deletion job is included.

For restore verification, use a pre-created, empty PostgreSQL database on a separately confirmed non-production cluster. Required variables:
- `SAAS_BACKUP_FILE` (the `.dump.age` archive).
- `SAAS_BACKUP_AGE_IDENTITY` (the protected private identity that matches the recipient key).
- `SAAS_RESTORE_VERIFY_DATABASE_URL`.
- `SAAS_RESTORE_VERIFY_EXPECTED_DB_NAME`.
- `SAAS_PRODUCTION_DB_NAME`.
- `TMPDIR` should point to a private temporary directory with sufficient free space for the decrypted dump.

The script verifies the checksum of the encrypted archive, decrypts it using the protected identity, checks the custom-format archive, rejects a database name equal to the configured production name, refuses a non-empty target schema, and restores in a single transaction without DROP/CLEAN. Unencrypted `.dump` files are refused unless `SAAS_BACKUP_ALLOW_UNENCRYPTED=true` is explicitly set for a controlled local test. It then checks required core SaaS tables. The name check cannot prove that a different DB name is not on the production cluster: the operator must independently verify the target host/cluster. Never run the verification script against production.

### Verified CI rehearsal

The Phase 9 CI workflow now provisions a disposable PostgreSQL 16 service, applies all migrations, generates an ephemeral `age` key pair, creates an encrypted custom-format backup, restores it into a separate empty database using the private test identity, and verifies the restored schema. The workflow uses PostgreSQL 16 client tools to match the service version. This validates encryption/decryption and restore in a clean ephemeral environment; it is **not** evidence of off-host storage, production key custody, long-term retention or a real disaster-recovery drill.

After a real restore drill, also compare expected row counts, sample critical records, validate payment/wallet invariants and record measured recovery time.

## Staging performance probe

A bounded, read-only load probe is available at `scripts/saas_staging_load_probe.py`. It is intended for an explicitly approved staging deployment only. It refuses HTTP URLs, local/private IPs, internal/test-only hostnames, path/query-bearing URLs, an unconfirmed hostname or missing non-production attestation. At runtime it resolves DNS and refuses any non-public address.

Required protected environment:

- `SAAS_LOAD_TEST_BASE_URL`: bare HTTPS origin, e.g. `https://staging-api.example.com`.
- `SAAS_LOAD_TEST_CONFIRM_HOST`: exact lowercase hostname from the URL.
- `SAAS_LOAD_TEST_APPROVAL=I_CONFIRM_NON_PRODUCTION_STAGING`: explicit operator attestation.
- Optional bounds: `SAAS_LOAD_TEST_DURATION_SECONDS` (1–120), `SAAS_LOAD_TEST_REQUESTS_PER_SECOND` (1–10) and `SAAS_LOAD_TEST_CONCURRENCY` (1–25).

Run:

```bash
python scripts/saas_staging_load_probe.py
```

It sends only GET requests to `/health` and `/ready`, does not follow redirects, and reports response counts plus p50/p95/max latency. It never prints response bodies, query strings, tokens or request headers. Any non-2xx response or network failure makes the command return non-zero.

This harness is a safe first-pass availability/latency check, not a benchmark or a full load/soak/failure test. Run only in a change-approved staging window, start with low limits, monitor database connections and host resources, and record the build, host, test bounds, observed latency and errors. Do not infer production capacity, SLOs or autoscaling thresholds from this probe.

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
