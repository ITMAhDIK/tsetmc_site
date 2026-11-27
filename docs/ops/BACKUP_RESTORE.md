# SaaS PostgreSQL backup, restore and off-host copy

All backup operations target only the dedicated SaaS PostgreSQL database.
Never point these scripts at Market, collector, TSETMC or investor-holdings
databases.

## Encrypted local backup

`scripts/saas_db_backup.sh` requires
`SAAS_BACKUP_DATABASE_URL`, `SAAS_EXPECTED_DB_NAME`, `SAAS_BACKUP_DIR`
and `SAAS_BACKUP_AGE_RECIPIENT`. It rejects inline URL passwords, requires
`sslmode=verify-full` for remote PostgreSQL, refuses to write inside the Git
checkout, creates a custom-format dump, checks it with `pg_restore --list`,
encrypts with the age public recipient and atomically publishes a
`.dump.age` plus SHA-256 sidecar using restrictive file permissions.

## Isolated restore drill

`scripts/saas_db_restore_verify.sh` verifies the SHA-256, decrypts using the
protected `SAAS_BACKUP_AGE_IDENTITY`, validates the archive, checks the target
database name and refuses the configured production database. The target must
be pre-created and empty; restore is single-transaction.

Required variables:

- `SAAS_BACKUP_FILE`
- `SAAS_RESTORE_VERIFY_DATABASE_URL`
- `SAAS_RESTORE_VERIFY_EXPECTED_DB_NAME`
- `SAAS_PRODUCTION_DB_NAME`
- `SAAS_BACKUP_AGE_IDENTITY`

The CI PostgreSQL job creates the SaaS schema, a dedicated read-only backup
role, age-encrypted backup and isolated scratch restore. The latest verified
CI run completed this drill successfully.

## Off-host publication

`scripts/saas_offsite_backup.sh` uses an externally configured rclone remote.
It accepts only age-encrypted `.dump.age` artifacts, verifies the local age
envelope and SHA-256 before transfer, uploads the archive and checksum with
`--immutable --checksum`, reads both objects back, and verifies the remote
archive bytes and checksum after transfer. It never overwrites or deletes
objects.

Systemd templates:

- `deploy/systemd/tloomberg-saas-offsite-backup.service`
- `deploy/systemd/tloomberg-saas-offsite-backup.timer`

The timer is scheduled daily in UTC. Install it only after configuring the
rclone remote, private credentials and protected age identity. Provider-side
object retention/object-lock must be configured and tested at the storage
provider; the script cannot create a legal immutability guarantee.

CI tests exercise remote copy/read/verification with a fake rclone executable.
They do not upload to an actual cloud bucket. Production scheduling, bucket
credentials, retention and restore access remain external release gates.
