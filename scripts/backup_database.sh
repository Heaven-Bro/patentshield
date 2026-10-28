#!/usr/bin/env bash
set -Eeuo pipefail

# Create an encrypted PostgreSQL custom-format backup; never emit plaintext dumps.
: "${DATABASE_URL:?Set DATABASE_URL for the database to back up}"
: "${AGE_RECIPIENT:?Set AGE_RECIPIENT to the approved backup recipient public key}"
command -v pg_dump >/dev/null || { echo "pg_dump is required" >&2; exit 2; }
command -v pg_restore >/dev/null || { echo "pg_restore is required" >&2; exit 2; }
command -v age >/dev/null || { echo "age is required to encrypt the backup" >&2; exit 2; }
OUT_DIR="${BACKUP_DIR:-./backups}"
mkdir -p "$OUT_DIR"
chmod 700 "$OUT_DIR"
umask 077
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
plain_tmp="$(mktemp "$OUT_DIR/.patentshield-${stamp}.XXXXXX.dump")"
service_tmp="$(mktemp "$OUT_DIR/.patentshield-${stamp}.XXXXXX.pgservice")"
encrypted="$OUT_DIR/patentshield-postgres-${stamp}.dump.age"
backup_complete=0
trap 'rm -f "$plain_tmp" "$service_tmp"; if [[ "$backup_complete" != "1" ]]; then rm -f "$encrypted" "$encrypted.sha256"; fi' EXIT
python scripts/prepare_pg_service.py --output "$service_tmp"
unset DATABASE_URL
PGSERVICEFILE="$service_tmp" pg_dump --format=custom --no-owner --no-acl --dbname=patentshield_backup --file="$plain_tmp"
pg_restore --list "$plain_tmp" >/dev/null
age --recipient "$AGE_RECIPIENT" --output "$encrypted" "$plain_tmp"
(cd "$OUT_DIR" && sha256sum "$(basename "$encrypted")" > "$(basename "$encrypted").sha256")
chmod 600 "$encrypted" "$encrypted.sha256"
backup_complete=1
printf 'Encrypted database backup: %s\n' "$encrypted"
printf 'Checksum file: %s.sha256\n' "$encrypted"
