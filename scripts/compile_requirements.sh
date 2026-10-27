#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

for manifest in requirements.in requirements-dev.in requirements-ml.in; do
  if [[ ! -f "$manifest" ]]; then
    printf 'Missing dependency input manifest: %s\n' "$manifest" >&2
    exit 2
  fi
done

# Keep lock tooling isolated from the application environment. The output text
# files are fully resolved and hash-pinned for the active pip-tools resolver.
TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/patentshield-lock.XXXXXX")"
trap 'rm -rf "$TEMP_DIR"' EXIT
python -m venv "$TEMP_DIR/venv"
LOCK_PYTHON="$TEMP_DIR/venv/bin/python"
"$LOCK_PYTHON" -m pip install --disable-pip-version-check "pip-tools==7.5.2"

PIP_COMPILE="$TEMP_DIR/venv/bin/pip-compile"
"$PIP_COMPILE" --resolver=backtracking --generate-hashes \
  --output-file requirements.txt requirements.in
"$PIP_COMPILE" --resolver=backtracking --generate-hashes \
  --constraint requirements.txt \
  --output-file requirements-dev.txt requirements-dev.in
"$PIP_COMPILE" --resolver=backtracking --generate-hashes \
  --constraint requirements.txt \
  --output-file requirements-ml.txt requirements-ml.in

printf '\nGenerated hash-locked manifests:\n'
printf '  requirements.txt\n  requirements-dev.txt\n  requirements-ml.txt\n'
printf 'Review and commit all three lock files. Run python scripts/verify_dependency_manifest.py next.\n'
