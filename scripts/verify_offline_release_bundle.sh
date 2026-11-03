#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUNDLE_DIR="${1:-$ROOT/patentshield-offline-release}"
COSIGN_PUBLIC_KEY="${COSIGN_PUBLIC_KEY:-$BUNDLE_DIR/cosign.pub}"
command -v cosign >/dev/null || { echo "cosign is required in the isolated zone" >&2; exit 2; }
command -v sha256sum >/dev/null || { echo "sha256sum is required" >&2; exit 2; }
command -v python >/dev/null || { echo "python is required" >&2; exit 2; }
[[ -r "$BUNDLE_DIR/SHA256SUMS" ]] || { echo "Bundle SHA256SUMS is missing" >&2; exit 2; }
[[ -r "$BUNDLE_DIR/SHA256SUMS.sigstore.json" ]] || { echo "Signed checksum manifest is missing" >&2; exit 2; }
[[ -r "$COSIGN_PUBLIC_KEY" ]] || { echo "Cosign public key is missing" >&2; exit 2; }

# Verify the release signing key fingerprint separately before trusting the key
# delivered with this bundle. The signed manifest verifies every payload file.
cosign verify-blob --key "$COSIGN_PUBLIC_KEY" --insecure-ignore-tlog=true \
  --bundle "$BUNDLE_DIR/SHA256SUMS.sigstore.json" "$BUNDLE_DIR/SHA256SUMS"
(
  cd "$BUNDLE_DIR"
  sha256sum -c SHA256SUMS
)
python "$ROOT/scripts/validate_offline_release_bundle.py" "$BUNDLE_DIR"
PYTHONPATH="$ROOT/backend${PYTHONPATH:+:$PYTHONPATH}" \
  python -m deployment.model_bundle verify --bundle "$BUNDLE_DIR/model-bundle"

while IFS=$'\t' read -r image_ref layout_path || [[ -n "${image_ref:-}" ]]; do
  [[ -z "${image_ref:-}" ]] && continue
  [[ "$image_ref" =~ ^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[a-f0-9]{64}$ ]] || {
    echo "Malformed digest reference in oci-images.tsv" >&2; exit 2;
  }
  layout="$BUNDLE_DIR/$layout_path"
  [[ -d "$layout" && -f "$layout/oci-layout" && -f "$layout/index.json" ]] || {
    echo "Missing or incomplete OCI layout: $layout_path" >&2; exit 2;
  }
  cosign verify --key "$COSIGN_PUBLIC_KEY" --offline --insecure-ignore-tlog=true \
    --local-image "$layout" >/dev/null
done < "$BUNDLE_DIR/oci-images.tsv"

if [[ -d "$BUNDLE_DIR/wheelhouse" ]]; then
  [[ -r "$BUNDLE_DIR/wheelhouse/SHA256SUMS" ]] || { echo "Wheelhouse checksum manifest is missing" >&2; exit 2; }
  (cd "$BUNDLE_DIR/wheelhouse" && sha256sum -c SHA256SUMS)
fi

echo "Offline release bundle signatures, files, OCI layouts, Helm chart, and all three model inventories verified."
