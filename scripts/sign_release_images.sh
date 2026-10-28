#!/usr/bin/env bash
set -Eeuo pipefail

# Sign immutable image references using a customer-controlled cosign key.
# The signing key must remain on the connected release-signing workstation.
: "${IMAGE_REFS_FILE:?Set IMAGE_REFS_FILE to a newline-separated file of image@sha256:digest references}"
: "${COSIGN_PRIVATE_KEY:?Set COSIGN_PRIVATE_KEY to the path of the cosign private key}"
command -v cosign >/dev/null || { echo "cosign is required" >&2; exit 2; }
[[ -r "$IMAGE_REFS_FILE" ]] || { echo "Cannot read IMAGE_REFS_FILE" >&2; exit 2; }
[[ -r "$COSIGN_PRIVATE_KEY" ]] || { echo "Cannot read COSIGN_PRIVATE_KEY" >&2; exit 2; }

while IFS= read -r image_ref || [[ -n "$image_ref" ]]; do
  image_ref="${image_ref%%#*}"
  image_ref="$(printf '%s' "$image_ref" | xargs)"
  [[ -z "$image_ref" ]] && continue
  [[ "$image_ref" =~ @sha256:[a-f0-9]{64}$ ]] || {
    echo "Refusing to sign a tag or malformed image reference: $image_ref" >&2
    exit 2
  }
  echo "Signing immutable image reference: ${image_ref%@*}@${image_ref##*@}"
  cosign sign --key "$COSIGN_PRIVATE_KEY" --tlog-upload=false --use-signing-config=false --yes "$image_ref"
done < "$IMAGE_REFS_FILE"
