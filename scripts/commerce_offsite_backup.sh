#!/usr/bin/env bash
# Publish one encrypted Commerce backup to an immutable off-host remote.
# Requires an externally configured rclone remote with object retention/lock.
set -Eeuo pipefail

: "${COMMERCE_BACKUP_REMOTE:?Set COMMERCE_BACKUP_REMOTE to an approved off-host rclone destination}"
: "${COMMERCE_BACKUP_DIR:?Set COMMERCE_BACKUP_DIR to the private local encrypted backup directory}"
command -v rclone >/dev/null 2>&1 || { printf 'Required command not found: rclone\n' >&2; exit 127; }
command -v sha256sum >/dev/null 2>&1 || { printf 'Required command not found: sha256sum\n' >&2; exit 127; }

case "$COMMERCE_BACKUP_REMOTE" in
  *:*) ;;
  *) printf 'COMMERCE_BACKUP_REMOTE must name an rclone remote, not a local path.\n' >&2; exit 2 ;;
esac
case "$COMMERCE_BACKUP_REMOTE" in
  *'..'*|*'?'*|*'#'*|*' '*)
    printf 'COMMERCE_BACKUP_REMOTE contains a disallowed path character.\n' >&2
    exit 2
    ;;
esac

backup_file="${1:-}"
if [[ -z "$backup_file" ]]; then
  backup_output="$(bash "$(dirname -- "${BASH_SOURCE[0]}")/commerce_db_backup.sh")"
  backup_file="$(printf '%s\n' "$backup_output" | sed -n 's/^Backup created: //p' | tail -n 1)"
fi
if [[ -z "$backup_file" || "$backup_file" != *.dump.age || ! -f "$backup_file" || -L "$backup_file" ]]; then
  printf 'A regular age-encrypted *.dump.age backup file is required.\n' >&2
  exit 2
fi

checksum_file="$backup_file.sha256"
if [[ ! -f "$checksum_file" || -L "$checksum_file" ]]; then
  printf 'Matching SHA-256 sidecar is missing or is not a regular file.\n' >&2
  exit 2
fi
if [[ ! -s "$backup_file" ]]; then
  printf 'Encrypted backup archive is empty.\n' >&2
  exit 2
fi

header="$(head -c 21 -- "$backup_file" 2>/dev/null || true)"
if [[ "$header" != "age-encryption.org/v1" ]]; then
  printf 'Refusing to upload an archive without an age encryption header.\n' >&2
  exit 2
fi

expected="$(tr -d '[:space:]' < "$checksum_file")"
actual="$(sha256sum -- "$backup_file" | awk '{print $1}')"
if [[ ! "$expected" =~ ^[a-fA-F0-9]{64}$ || "$actual" != "$expected" ]]; then
  printf 'Local backup SHA-256 verification failed.\n' >&2
  exit 3
fi

remote_archive="${COMMERCE_BACKUP_REMOTE%/}/$(basename -- "$backup_file")"
remote_checksum="$remote_archive.sha256"

# --immutable prevents overwriting an existing backup object. The script
# never deletes remote objects: retention/object-lock policy is configured
# at the storage provider, not inferred or modified here.
rclone copyto --immutable --checksum --retries 3 --low-level-retries 10 \
  "$backup_file" "$remote_archive"
rclone copyto --immutable --checksum --retries 3 --low-level-retries 10 \
  "$checksum_file" "$remote_checksum"

rclone lsf "$remote_archive" >/dev/null
rclone lsf "$remote_checksum" >/dev/null

# Read both remote objects back through rclone and verify their actual content,
# not only their existence or the metadata reported by the storage provider.
remote_actual="$(rclone cat "$remote_archive" | sha256sum | awk '{print $1}')"
remote_sidecar="$(rclone cat "$remote_checksum" | tr -d '[:space:]')"
if [[ "$remote_actual" != "$expected" || "$remote_sidecar" != "$expected" ]]; then
  printf 'Off-host backup content verification failed.\n' >&2
  exit 4
fi

printf 'Off-host encrypted backup uploaded and verified: %s\n' "$remote_archive"
printf 'Off-host checksum uploaded: %s\n' "$remote_checksum"
