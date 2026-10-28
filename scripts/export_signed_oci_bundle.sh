#!/usr/bin/env bash
set -Eeuo pipefail

# Run on a connected staging workstation after signing images. Produces OCI
# layouts that can be verified without registry access in the air-gapped zone.
: "${IMAGE_REFS_FILE:?Set IMAGE_REFS_FILE to newline-separated image@sha256:digest references}"
: "${COSIGN_PUBLIC_KEY:?Set COSIGN_PUBLIC_KEY to the cosign public key file}"
: "${COSIGN_PRIVATE_KEY:?Set COSIGN_PRIVATE_KEY to the offline release manifest signing key}"
: "${MODEL_BUNDLE_DIR:?Set MODEL_BUNDLE_DIR to an already exported model bundle}"
OUT_DIR="${OUT_DIR:-./patentshield-offline-release}"
if [[ -d "$OUT_DIR" && -n "$(find "$OUT_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
  echo "Output directory must be empty or absent: $OUT_DIR" >&2
  exit 2
fi
command -v cosign >/dev/null || { echo "cosign is required" >&2; exit 2; }
command -v sha256sum >/dev/null || { echo "sha256sum is required" >&2; exit 2; }
[[ -r "$IMAGE_REFS_FILE" && -r "$COSIGN_PUBLIC_KEY" ]] || { echo "Image refs/public key are not readable" >&2; exit 2; }
PYTHONPATH="${PYTHONPATH:-}:backend" python -m deployment.model_bundle verify --bundle "$MODEL_BUNDLE_DIR"
mkdir -p "$OUT_DIR/oci"
cp "$COSIGN_PUBLIC_KEY" "$OUT_DIR/cosign.pub"
cp "$IMAGE_REFS_FILE" "$OUT_DIR/image-refs.txt"
cp -a "$MODEL_BUNDLE_DIR" "$OUT_DIR/model-bundle"
: > "$OUT_DIR/oci-images.tsv"

while IFS= read -r image_ref || [[ -n "$image_ref" ]]; do
  image_ref="${image_ref%%#*}"
  image_ref="$(printf '%s' "$image_ref" | xargs)"
  [[ -z "$image_ref" ]] && continue
  [[ "$image_ref" =~ @sha256:[a-f0-9]{64}$ ]] || {
    echo "Refusing to export non-digest image reference: $image_ref" >&2
    exit 2
  }
  cosign verify --key "$COSIGN_PUBLIC_KEY" --insecure-ignore-tlog=true "$image_ref" >/dev/null
  safe_name="$(printf '%s' "$image_ref" | tr -c 'A-Za-z0-9._-' '_' | cut -c1-48)-$(printf '%s' "$image_ref" | sha256sum | cut -c1-12)"
  target="$OUT_DIR/oci/$safe_name"
  mkdir -p "$target"
  cosign save "$image_ref" --dir "$target"
  cosign verify --key "$COSIGN_PUBLIC_KEY" --offline --insecure-ignore-tlog=true --local-image "$target" >/dev/null
  printf '%s\t%s\n' "$image_ref" "oci/$safe_name" >> "$OUT_DIR/oci-images.tsv"
done < "$IMAGE_REFS_FILE"

(
  cd "$OUT_DIR"
  find . -type f ! -name SHA256SUMS ! -name SHA256SUMS.sigstore.json -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS
  sha256sum -c SHA256SUMS >/dev/null
  cosign sign-blob --key "$COSIGN_PRIVATE_KEY" --tlog-upload=false --bundle SHA256SUMS.sigstore.json SHA256SUMS
  cosign verify-blob --key "$COSIGN_PUBLIC_KEY" --insecure-ignore-tlog=true --bundle SHA256SUMS.sigstore.json SHA256SUMS >/dev/null
)
printf 'Created verified air-gap release bundle: %s\n' "$OUT_DIR"
