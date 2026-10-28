#!/usr/bin/env bash
set -Eeuo pipefail

# Safe restore into a pre-created, approved target database. It does not drop
# objects or recreate a database; destructive recovery is a separate DBA action.
: "${DATABASE_URL:?Set DATABASE_URL to the approved restore target}"
: "${AGE_IDENTITY:?Set AGE_IDENTITY to the backup decryption identity file}"
: "${RESTORE_CONFIRM:?Set RESTORE_CONFIRM=I_HAVE_VERIFIED_TARGET_DATABASE after checking the target}"
[[ "$RESTORE_CONFIRM" == "I_HAVE_VERIFIED_TARGET_DATABASE" ]] || {
  echo "Refusing restore: explicit target confirmation is required." >&2; exit 2;
}
[[ $# -eq 1 && -r "$1" ]] || { echo "Usage: $0 path/to/backup.dump.age" >&2; exit 2; }
command -v pg_restore >/dev/null || { echo "pg_restore is required" >&2; exit 2; }
command -v age >/dev/null || { echo "age is required" >&2; exit 2; }
backup="$1"
[[ -r "$backup.sha256" ]] || { echo "Backup checksum file is missing" >&2; exit 2; }
(cd "$(dirname "$backup")" && sha256sum -c "$(basename "$backup").sha256")
umask 077
tmp="$(mktemp "${TMPDIR:-/tmp}/patentshield-restore.XXXXXX.dump")"
service_tmp="$(mktemp "${TMPDIR:-/tmp}/patentshield-restore.XXXXXX.pgservice")"
trap 'rm -f "$tmp" "$service_tmp"' EXIT
python scripts/prepare_pg_service.py --output "$service_tmp"
unset DATABASE_URL
age --decrypt --identity "$AGE_IDENTITY" --output "$tmp" "$backup"
pg_restore --list "$tmp" >/dev/null
PGSERVICEFILE="$service_tmp" pg_restore --no-owner --no-acl --dbname=patentshield_backup "$tmp"
echo "Restore command completed. Run application, row-count, tenant-isolation and evidence-integrity checks before reopening traffic."
