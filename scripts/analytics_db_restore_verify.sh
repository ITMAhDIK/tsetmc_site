#!/usr/bin/env bash
# Verify and restore a Analytics backup into a pre-created, empty verification DB only.
# Never target the production database or a market-data database.
set -Eeuo pipefail

: "${ANALYTICS_BACKUP_FILE:?Set ANALYTICS_BACKUP_FILE to the backup archive}"
: "${ANALYTICS_RESTORE_VERIFY_DATABASE_URL:?Set the isolated restore-test database connection}"
: "${ANALYTICS_RESTORE_VERIFY_EXPECTED_DB_NAME:?Set the expected restore-test database name}"
: "${ANALYTICS_PRODUCTION_DB_NAME:?Set the production Analytics database name for the no-target-equality guard}"

for command_name in psql pg_restore sha256sum awk; do
  command -v "$command_name" >/dev/null 2>&1 || {
    printf 'Required command not found: %s\n' "$command_name" >&2
    exit 127
  }
done

if [[ "$ANALYTICS_RESTORE_VERIFY_DATABASE_URL" == *"password="* || "$ANALYTICS_RESTORE_VERIFY_DATABASE_URL" =~ ^[[:alpha:]][[:alnum:]+.-]*://[^/]*:[^/@]*@ ]]; then
  printf 'Do not embed a database password in the connection string; use a protected .pgpass file.\n' >&2
  exit 2
fi

[[ -f "$ANALYTICS_BACKUP_FILE" ]] || {
  printf 'Backup file does not exist: %s\n' "$ANALYTICS_BACKUP_FILE" >&2
  exit 2
}
[[ -f "$ANALYTICS_BACKUP_FILE.sha256" ]] || {
  printf 'Backup checksum file is missing: %s.sha256\n' "$ANALYTICS_BACKUP_FILE" >&2
  exit 2
}

expected_checksum="$(cat -- "$ANALYTICS_BACKUP_FILE.sha256")"
if [[ ! "$expected_checksum" =~ ^[a-fA-F0-9]{64}$ ]]; then
  printf 'Backup checksum file is malformed.\n' >&2
  exit 3
fi
actual_checksum="$(sha256sum "$ANALYTICS_BACKUP_FILE" | awk '{print $1}')"
if [[ "$actual_checksum" != "$expected_checksum" ]]; then
  printf 'Backup checksum verification failed.\n' >&2
  exit 3
fi

restore_archive="$ANALYTICS_BACKUP_FILE"
tmp_dump=""
cleanup() {
  [[ -z "$tmp_dump" ]] || rm -f -- "$tmp_dump"
}
trap cleanup EXIT
umask 077

case "$ANALYTICS_BACKUP_FILE" in
  *.dump.age)
    : "${ANALYTICS_BACKUP_AGE_IDENTITY:?Set ANALYTICS_BACKUP_AGE_IDENTITY to the protected private age identity}"
    command -v age >/dev/null 2>&1 || {
      printf 'Required command not found: age\n' >&2
      exit 127
    }
    [[ -f "$ANALYTICS_BACKUP_AGE_IDENTITY" ]] || {
      printf 'The age private identity file does not exist.\n' >&2
      exit 2
    }
    tmp_dump="$(mktemp "${TMPDIR:-/tmp}/analytics-restore.XXXXXX.dump")"
    rm -f -- "$tmp_dump"
    age --decrypt --identity "$ANALYTICS_BACKUP_AGE_IDENTITY" --output "$tmp_dump" "$ANALYTICS_BACKUP_FILE"
    restore_archive="$tmp_dump"
    ;;
  *.dump)
    if [[ "${ANALYTICS_BACKUP_ALLOW_UNENCRYPTED:-false}" != "true" ]]; then
      printf 'Refusing an unencrypted backup; set ANALYTICS_BACKUP_ALLOW_UNENCRYPTED=true only for a controlled local test.\n' >&2
      exit 4
    fi
    ;;
  *)
    printf 'Unsupported backup archive extension. Expected .dump.age or .dump.\n' >&2
    exit 4
    ;;
esac

pg_restore --list "$restore_archive" >/dev/null

# Remote PostgreSQL restore targets must verify both the CA chain and hostname.
# Loopback-only disposable/local test databases may omit TLS.
dsn_host=""
if [[ "$ANALYTICS_RESTORE_VERIFY_DATABASE_URL" =~ ^postgres(ql)?://([^/@?]+@)?(\[[^]]+\]|[^:/?]+) ]]; then
  dsn_host="${BASH_REMATCH[3]}"
  dsn_host="${dsn_host#[}"
  dsn_host="${dsn_host%]}"
elif [[ "$ANALYTICS_RESTORE_VERIFY_DATABASE_URL" =~ (^|[[:space:]])host=([^[:space:]]+) ]]; then
  dsn_host="${BASH_REMATCH[2]}"
else
  dsn_host="localhost"
fi
for host in ${dsn_host//,/ }; do
  case "$host" in
    localhost|127.0.0.1|::1) ;;
    *)
      if [[ "$ANALYTICS_RESTORE_VERIFY_DATABASE_URL" != *"sslmode=verify-full"* ]]; then
        printf 'Remote restore target connections must explicitly use sslmode=verify-full.\n' >&2
        exit 4
      fi
      ;;
  esac
done

target_db="$(psql "$ANALYTICS_RESTORE_VERIFY_DATABASE_URL" -X -A -t -q -c 'SELECT current_database()')"
if [[ "$target_db" != "$ANALYTICS_RESTORE_VERIFY_EXPECTED_DB_NAME" ]]; then
  printf 'Restore target database name does not match the expected verification database.\n' >&2
  exit 5
fi
if [[ "$target_db" == "$ANALYTICS_PRODUCTION_DB_NAME" ]]; then
  printf 'Refusing to restore into the production database.\n' >&2
  exit 6
fi

existing_objects="$(psql "$ANALYTICS_RESTORE_VERIFY_DATABASE_URL" -X -A -t -q -c \
  "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','p','v','m','S','f')")"
if [[ "$existing_objects" != "0" ]]; then
  printf 'Restore target is not empty; no objects were changed.\n' >&2
  exit 7
fi

# --single-transaction ensures a failed restore rolls back instead of leaving a partial schema.
pg_restore --exit-on-error --single-transaction --no-owner --no-acl \
  --dbname="$ANALYTICS_RESTORE_VERIFY_DATABASE_URL" "$restore_archive"

required_tables_ok="$(psql "$ANALYTICS_RESTORE_VERIFY_DATABASE_URL" -X -A -t -q -c \
  "SELECT (to_regclass('public.analytics_consents') IS NOT NULL AND to_regclass('public.analytics_consent_history') IS NOT NULL AND to_regclass('public.analytics_events') IS NOT NULL AND to_regclass('public.analytics_audit_logs') IS NOT NULL AND to_regclass('public.analytics_ingestion_limits') IS NOT NULL AND to_regclass('public.analytics_schema_migrations') IS NOT NULL)")"
if [[ "$required_tables_ok" != "t" ]]; then
  printf 'Restore finished, but one or more required Analytics tables are missing.\n' >&2
  exit 8
fi

printf 'Restore verification passed for isolated database: %s\n' "$target_db"
