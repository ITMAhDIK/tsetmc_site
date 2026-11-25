#!/usr/bin/env bash
# Verify and restore a SaaS backup into a pre-created, empty verification DB only.
# Never target the production database or a market-data database.
set -Eeuo pipefail

: "${SAAS_BACKUP_FILE:?Set SAAS_BACKUP_FILE to the backup archive}"
: "${SAAS_RESTORE_VERIFY_DATABASE_URL:?Set the isolated restore-test database connection}"
: "${SAAS_RESTORE_VERIFY_EXPECTED_DB_NAME:?Set the expected restore-test database name}"
: "${SAAS_PRODUCTION_DB_NAME:?Set the production SaaS database name for the no-target-equality guard}"

for command_name in psql pg_restore sha256sum awk; do
  command -v "$command_name" >/dev/null 2>&1 || {
    printf 'Required command not found: %s\n' "$command_name" >&2
    exit 127
  }
done

if [[ "$SAAS_RESTORE_VERIFY_DATABASE_URL" == *"password="* || "$SAAS_RESTORE_VERIFY_DATABASE_URL" =~ ^[[:alpha:]][[:alnum:]+.-]*://[^/]*:[^/@]*@ ]]; then
  printf 'Do not embed a database password in the connection string; use a protected .pgpass file.\n' >&2
  exit 2
fi

[[ -f "$SAAS_BACKUP_FILE" ]] || {
  printf 'Backup file does not exist: %s\n' "$SAAS_BACKUP_FILE" >&2
  exit 2
}
[[ -f "$SAAS_BACKUP_FILE.sha256" ]] || {
  printf 'Backup checksum file is missing: %s.sha256\n' "$SAAS_BACKUP_FILE" >&2
  exit 2
}

expected_checksum="$(cat -- "$SAAS_BACKUP_FILE.sha256")"
if [[ ! "$expected_checksum" =~ ^[a-fA-F0-9]{64}$ ]]; then
  printf 'Backup checksum file is malformed.\n' >&2
  exit 3
fi
actual_checksum="$(sha256sum "$SAAS_BACKUP_FILE" | awk '{print $1}')"
if [[ "$actual_checksum" != "$expected_checksum" ]]; then
  printf 'Backup checksum verification failed.\n' >&2
  exit 3
fi

restore_archive="$SAAS_BACKUP_FILE"
tmp_dump=""
cleanup() {
  [[ -z "$tmp_dump" ]] || rm -f -- "$tmp_dump"
}
trap cleanup EXIT
umask 077

case "$SAAS_BACKUP_FILE" in
  *.dump.age)
    : "${SAAS_BACKUP_AGE_IDENTITY:?Set SAAS_BACKUP_AGE_IDENTITY to the protected private age identity}"
    command -v age >/dev/null 2>&1 || {
      printf 'Required command not found: age\n' >&2
      exit 127
    }
    [[ -f "$SAAS_BACKUP_AGE_IDENTITY" ]] || {
      printf 'The age private identity file does not exist.\n' >&2
      exit 2
    }
    tmp_dump="$(mktemp "${TMPDIR:-/tmp}/saas-restore.XXXXXX.dump")"
    age --decrypt --identity "$SAAS_BACKUP_AGE_IDENTITY" --output "$tmp_dump" "$SAAS_BACKUP_FILE"
    restore_archive="$tmp_dump"
    ;;
  *.dump)
    if [[ "${SAAS_BACKUP_ALLOW_UNENCRYPTED:-false}" != "true" ]]; then
      printf 'Refusing an unencrypted backup; set SAAS_BACKUP_ALLOW_UNENCRYPTED=true only for a controlled local test.\n' >&2
      exit 4
    fi
    ;;
  *)
    printf 'Unsupported backup archive extension. Expected .dump.age or .dump.\n' >&2
    exit 4
    ;;
esac

pg_restore --list "$restore_archive" >/dev/null

target_db="$(psql "$SAAS_RESTORE_VERIFY_DATABASE_URL" -X -A -t -q -c 'SELECT current_database()')"
if [[ "$target_db" != "$SAAS_RESTORE_VERIFY_EXPECTED_DB_NAME" ]]; then
  printf 'Restore target database name does not match the expected verification database.\n' >&2
  exit 5
fi
if [[ "$target_db" == "$SAAS_PRODUCTION_DB_NAME" ]]; then
  printf 'Refusing to restore into the production database.\n' >&2
  exit 6
fi

existing_objects="$(psql "$SAAS_RESTORE_VERIFY_DATABASE_URL" -X -A -t -q -c \
  "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','p','v','m','S','f')")"
if [[ "$existing_objects" != "0" ]]; then
  printf 'Restore target is not empty; no objects were changed.\n' >&2
  exit 7
fi

# --single-transaction ensures a failed restore rolls back instead of leaving a partial schema.
pg_restore --exit-on-error --single-transaction --no-owner --no-acl \
  --dbname="$SAAS_RESTORE_VERIFY_DATABASE_URL" "$restore_archive"

required_tables_ok="$(psql "$SAAS_RESTORE_VERIFY_DATABASE_URL" -X -A -t -q -c \
  "SELECT (to_regclass('public.users') IS NOT NULL AND to_regclass('public.sessions') IS NOT NULL AND to_regclass('public.payment_orders') IS NOT NULL AND to_regclass('public.wallet_ledger_entries') IS NOT NULL AND to_regclass('public.admin_audit_logs') IS NOT NULL AND to_regclass('public.saas_schema_migrations') IS NOT NULL)")"
if [[ "$required_tables_ok" != "t" ]]; then
  printf 'Restore finished, but one or more required SaaS tables are missing.\n' >&2
  exit 8
fi

printf 'Restore verification passed for isolated database: %s\n' "$target_db"
