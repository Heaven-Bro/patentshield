#!/usr/bin/env bash
set -Eeuo pipefail

BUNDLE_DIR="${1:-./patentshield-offline-release}"
COSIGN_PUBLIC_KEY="${COSIGN_PUBLIC_KEY:-$BUNDLE_DIR/cosign.pub}"
command -v cosign >/dev/null || { echo "cosign is required in the isolated zone" >&2; exit 2; }
command -v sha256sum >/dev/null || { echo "sha256sum is required" >&2; exit 2; }
[[ -r "$BUNDLE_DIR/SHA256SUMS" ]] || { echo "Bundle SHA256SUMS is missing" >&2; exit 2; }
[[ -r "$BUNDLE_DIR/SHA256SUMS.sigstore.json" ]] || { echo "Signed checksum manifest is missing" >&2; exit 2; }
[[ -r "$COSIGN_PUBLIC_KEY" ]] || { echo "Cosign public key is missing" >&2; exit 2; }
cosign verify-blob --key "$COSIGN_PUBLIC_KEY" --insecure-ignore-tlog=true --bundle "$BUNDLE_DIR/SHA256SUMS.sigstore.json" "$BUNDLE_DIR/SHA256SUMS"
(
  cd "$BUNDLE_DIR"
  sha256sum -c SHA256SUMS
)
PYTHONPATH="${PYTHONPATH:-}:backend" python -m deployment.model_bundle verify --bundle "$BUNDLE_DIR/model-bundle"
while IFS=$'\t' read -r image_ref layout_path || [[ -n "${image_ref:-}" ]]; do
  [[ -z "${image_ref:-}" ]] && continue
  [[ "$image_ref" =~ @sha256:[a-f0-9]{64}$ ]] || { echo "Malformed digest reference in oci-images.tsv" >&2; exit 2; }
  layout="$BUNDLE_DIR/$layout_path"
  [[ -d "$layout" ]] || { echo "Missing OCI layout: $layout" >&2; exit 2; }
  echo "Offline signature verification: $image_ref"
  cosign verify --key "$COSIGN_PUBLIC_KEY" --offline --insecure-ignore-tlog=true --local-image "$layout"
done < "$BUNDLE_DIR/oci-images.tsv"
echo "Offline release bundle checksum, model hashes, and OCI signatures verified."
