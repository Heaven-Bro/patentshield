#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WHEELHOUSE_DIR="${WHEELHOUSE_DIR:-$ROOT/offline/wheelhouse}"
[[ $# -eq 1 ]] || { echo "Usage: $0 requirements.txt|requirements-ml.txt|requirements-dev.txt" >&2; exit 2; }
LOCK="$1"
case "$LOCK" in
  requirements.txt|requirements-ml.txt|requirements-dev.txt) ;;
  *) echo "Refusing unapproved requirements path: $LOCK" >&2; exit 2 ;;
esac
[[ -r "$ROOT/$LOCK" ]] || { echo "Requirements lock is missing: $LOCK" >&2; exit 2; }
[[ -r "$WHEELHOUSE_DIR/SHA256SUMS" ]] || { echo "Wheelhouse checksum manifest is missing" >&2; exit 2; }
python "$ROOT/scripts/check_requirement_hashes.py" "$ROOT/$LOCK"
(
  cd "$WHEELHOUSE_DIR"
  sha256sum -c SHA256SUMS
)
python "$ROOT/scripts/verify_offline_wheelhouse.py" "$WHEELHOUSE_DIR" "$ROOT/$LOCK"
python -m pip install --no-index --find-links "$WHEELHOUSE_DIR" --require-hashes -r "$ROOT/$LOCK"
echo "Installed $LOCK from the verified local wheelhouse without index access."
