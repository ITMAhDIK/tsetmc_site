# SaaS PostgreSQL backup and restore

Only `SAAS_DATABASE_URL` is honored. The tool never falls back to
`DATABASE_URL`, refuses an identical `MARKET_DATABASE_URL`, and passes the
connection URL to PostgreSQL tools via `PGDATABASE`, not command-line arguments.

## Backup and verification

```bash
export SAAS_DATABASE_URL='postgresql://...'
python tools/saas_db_ops.py backup --directory /secure/backup/tloomberg-saas
python tools/saas_db_ops.py verify --file /secure/backup/tloomberg-saas/saas-YYYYMMDDTHHMMSSZ.dump
```

The archive is custom-format, written to a temporary file, parsed with
`pg_restore --list`, atomically published and accompanied by a SHA-256 manifest.
POSIX directories/files are restricted to owner-only permissions. Encryption,
offsite upload and retention scheduling belong to the hosting environment.

## Destructive restore

1. Confirm the target is SaaS, never market data.
2. Verify archive/manifest and take a recent pre-restore backup.
3. Obtain an approved change/incident reference and second-person review.
4. Point `SAAS_DATABASE_URL` at the intended target.
5. Require the exact confirmation literal:

```bash
python tools/saas_db_ops.py restore --file /secure/backup/tloomberg-saas/saas-YYYYMMDDTHHMMSSZ.dump --confirm-restore RESTORE_SAAS_DATABASE
python tools/saas_healthcheck.py
```

Restore uses `pg_restore --clean --if-exists --no-owner --no-acl --exit-on-error`;
it does not create/drop the database. Reconcile login, entitlements, payment
orders, wallet totals, withdrawal reservations and audit after restore.

Suggested starting policy: daily encrypted backups, 30-day daily retention,
monthly archival, quarterly isolated restore drills, and documented RPO/RTO.
These are recommendations, not an implemented scheduler or legal requirement.
