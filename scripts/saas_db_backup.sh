#!/usr/bin/env bash
# Create a private, checksum-verified custom-format backup of the dedicated SaaS DB.
# Never point SAAS_DATABASE_URL at a market-data database.
set -Eeuo pipefail

: "${SAAS_DATABASE_URL:?Set SAAS_DATABASE_URL to the dedicated SaaS PostgreSQL database}"
: "${SAAS_EXPECTED_DB_NAME:?Set SAAS_EXPECTED_DB_NAME to the expected SaaS database name}"
: "${SAAS_BACKUP_DIR:?Set SAAS_BACKUP_DIR to a private directory outside the repository}"

for command_name in psql pg_dump pg_restore sha256sum awk; do
  command -v "$command_name" >/dev/null 2>&1 || {
    printf 'Required command not found: %s\n' "$command_name" >&2
    exit 127
  }
done

mkdir -p -- "$SAAS_BACKUP_DIR"
backup_dir="$(cd -- "$SAAS_BACKUP_DIR" && pwd -P)"

# Do not expose a database password through command-line process arguments.
if [[ "$SAAS_DATABASE_URL" == *"password="* || "$SAAS_DATABASE_URL" =~ ^[[:alpha:]][[:alnum:]+.-]*://[^/]*:[^/@]*@ ]]; then
  printf 'Do not embed a database password in the connection string; use a protected .pgpass file.\n' >&2
  exit 2
fi

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

actual_db="$(psql "$SAAS_DATABASE_URL" -X -A -t -q -c 'SELECT current_database()')"
if [[ "$actual_db" != "$SAAS_EXPECTED_DB_NAME" ]]; then
  printf 'Database safety check failed: connected database name does not match SAAS_EXPECTED_DB_NAME.\n' >&2
  exit 3
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
base_name="tloomberg-saas-${timestamp}-$$"
final_dump="$backup_dir/$base_name.dump"
final_checksum="$final_dump.sha256"

if [[ -e "$final_dump" || -e "$final_checksum" ]]; then
  printf 'Refusing to overwrite an existing backup: %s\n' "$final_dump" >&2
  exit 4
fi

tmp_dump="$(mktemp "$backup_dir/.saas-backup.XXXXXX")"
tmp_checksum="$tmp_dump.sha256"
cleanup() {
  rm -f -- "$tmp_dump" "$tmp_checksum"
}
trap cleanup EXIT

umask 077
pg_dump --format=custom --no-owner --no-acl --file="$tmp_dump" "$SAAS_DATABASE_URL"
pg_restore --list "$tmp_dump" >/dev/null
sha256sum "$tmp_dump" | awk '{print $1}' > "$tmp_checksum"
chmod 600 "$tmp_dump" "$tmp_checksum"

mv -- "$tmp_dump" "$final_dump"
tmp_dump=""
mv -- "$tmp_checksum" "$final_checksum"
tmp_checksum=""

printf 'Backup created: %s\n' "$final_dump"
printf 'SHA-256 file: %s\n' "$final_checksum"
