#!/usr/bin/env bash
set -Eeuo pipefail

# Download hash-locked Python wheels on a connected workstation. Wheelhouse must
# match the target Python version, OS, CPU architecture, and libc environment.
DEST="${WHEELHOUSE_DIR:-./offline/wheelhouse}"
mkdir -p "$DEST"
if [[ "${INCLUDE_ML_DEPS:-0}" == "1" ]]; then
  python -m pip download --require-hashes -r requirements-ml.txt --dest "$DEST"
else
  python -m pip download --require-hashes -r requirements.txt --dest "$DEST"
fi
if [[ "${INCLUDE_DEV_DEPS:-0}" == "1" ]]; then
  python -m pip download --require-hashes -r requirements-dev.txt --dest "$DEST"
fi
(
  cd "$DEST"
  find . -maxdepth 1 -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS
  sha256sum -c SHA256SUMS
)
printf 'Offline wheelhouse prepared at %s\n' "$DEST"
