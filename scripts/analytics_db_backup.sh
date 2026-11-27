#!/usr/bin/env bash
# Create a private, checksum-verified, age-encrypted custom-format Analytics DB backup.
# Never point ANALYTICS_BACKUP_DATABASE_URL at a market-data database.
set -Eeuo pipefail

: "${ANALYTICS_BACKUP_DATABASE_URL:?Set ANALYTICS_BACKUP_DATABASE_URL to the dedicated Analytics PostgreSQL database}"
: "${ANALYTICS_EXPECTED_DB_NAME:?Set ANALYTICS_EXPECTED_DB_NAME to the expected Analytics database name}"
: "${ANALYTICS_BACKUP_DIR:?Set ANALYTICS_BACKUP_DIR to a private directory outside the repository}"
: "${ANALYTICS_BACKUP_AGE_RECIPIENT:?Set the age public recipient; do not use a private identity here}"

for command_name in psql pg_dump pg_restore sha256sum awk age; do
  command -v "$command_name" >/dev/null 2>&1 || {
    printf 'Required command not found: %s\n' "$command_name" >&2
    exit 127
  }
done

# Do not expose a database password through command-line process arguments.
if [[ "$ANALYTICS_BACKUP_DATABASE_URL" == *"password="* || "$ANALYTICS_BACKUP_DATABASE_URL" =~ ^[[:alpha:]][[:alnum:]+.-]*://[^/]*:[^/@]*@ ]]; then
  printf 'Do not embed a database password in the connection string; use a protected .pgpass file.\n' >&2
  exit 2
fi

mkdir -p -- "$ANALYTICS_BACKUP_DIR"
backup_dir="$(cd -- "$ANALYTICS_BACKUP_DIR" && pwd -P)"

# Protect against accidentally committing database backups to this Git checkout.
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if repo_root="$(git -C "$script_dir" rev-parse --show-toplevel 2>/dev/null)"; then
  repo_root="$(cd -- "$repo_root" && pwd -P)"
  case "$backup_dir/" in
    "$repo_root/"*)
      printf 'Refusing to write backups inside the Git repository: %s\n' "$backup_dir" >&2
      exit 2
      ;;
  esac
fi

# Remote PostgreSQL backups must verify both the CA chain and the server hostname.
# Loopback-only disposable/local test databases may omit TLS.
dsn_host=""
if [[ "$ANALYTICS_BACKUP_DATABASE_URL" =~ ^postgres(ql)?://([^/@?]+@)?(\[[^]]+\]|[^:/?]+) ]]; then
  dsn_host="${BASH_REMATCH[3]}"
  dsn_host="${dsn_host#[}"
  dsn_host="${dsn_host%]}"
elif [[ "$ANALYTICS_BACKUP_DATABASE_URL" =~ (^|[[:space:]])host=([^[:space:]]+) ]]; then
  dsn_host="${BASH_REMATCH[2]}"
else
  dsn_host="localhost"
fi
for host in ${dsn_host//,/ }; do
  case "$host" in
    localhost|127.0.0.1|::1) ;;
    *)
      if [[ "$ANALYTICS_BACKUP_DATABASE_URL" != *"sslmode=verify-full"* ]]; then
        printf 'Remote backup database connections must explicitly use sslmode=verify-full.\n' >&2
        exit 2
      fi
      ;;
  esac
done

actual_db="$(psql "$ANALYTICS_BACKUP_DATABASE_URL" -X -A -t -q -c 'SELECT current_database()')"
if [[ "$actual_db" != "$ANALYTICS_EXPECTED_DB_NAME" ]]; then
  printf 'Database safety check failed: connected database name does not match ANALYTICS_EXPECTED_DB_NAME.\n' >&2
  exit 3
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
base_name="tloomberg-analytics-${timestamp}-$$"
final_archive="$backup_dir/$base_name.dump.age"
final_checksum="$final_archive.sha256"

if [[ -e "$final_archive" || -e "$final_checksum" ]]; then
  printf 'Refusing to overwrite an existing backup: %s\n' "$final_archive" >&2
  exit 4
fi

tmp_dump="$(mktemp "$backup_dir/.analytics-dump.XXXXXX")"
tmp_archive="$(mktemp "$backup_dir/.analytics-archive.XXXXXX")"
tmp_checksum="$tmp_archive.sha256"
cleanup() {
  [[ -z "$tmp_dump" ]] || rm -f -- "$tmp_dump"
  [[ -z "$tmp_archive" ]] || rm -f -- "$tmp_archive"
  [[ -z "$tmp_checksum" ]] || rm -f -- "$tmp_checksum"
}
trap cleanup EXIT

umask 077
pg_dump --format=custom --no-owner --no-acl --file="$tmp_dump" "$ANALYTICS_BACKUP_DATABASE_URL"
pg_restore --list "$tmp_dump" >/dev/null

rm -f -- "$tmp_archive"
age --encrypt --recipient "$ANALYTICS_BACKUP_AGE_RECIPIENT" --output "$tmp_archive" "$tmp_dump"

sha256sum "$tmp_archive" | awk '{print $1}' > "$tmp_checksum"
chmod 600 "$tmp_archive" "$tmp_checksum"

mv -- "$tmp_archive" "$final_archive"
tmp_archive=""
mv -- "$tmp_checksum" "$final_checksum"
tmp_checksum=""
rm -f -- "$tmp_dump"
tmp_dump=""

printf 'Backup created: %s\n' "$final_archive"
printf 'SHA-256 file: %s\n' "$final_checksum"
printf 'Encryption: age (recipient key only; private identity was not accessed)\n'
