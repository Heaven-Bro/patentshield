#!/usr/bin/env bash
set -Eeuo pipefail

# Connected release workstation: export digest-pinned/signed OCI images, the
# exact Helm chart, verified model artifacts, and optionally a hash-locked wheelhouse.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${IMAGE_REFS_FILE:?Set IMAGE_REFS_FILE to newline-separated image@sha256:<digest> references}"
: "${COSIGN_PUBLIC_KEY:?Set COSIGN_PUBLIC_KEY to the cosign public key file}"
: "${COSIGN_PRIVATE_KEY:?Set COSIGN_PRIVATE_KEY to the offline bundle signing key}"
: "${MODEL_BUNDLE_DIR:?Set MODEL_BUNDLE_DIR to a verified 3-model bundle}"
: "${BACKEND_IMAGE_REF:?Set BACKEND_IMAGE_REF to the digest-pinned API/worker/Beat image reference}"
: "${FRONTEND_IMAGE_REF:?Set FRONTEND_IMAGE_REF to the digest-pinned frontend image reference}"
: "${POSTGRES_IMAGE_REF:?Set POSTGRES_IMAGE_REF to the digest-pinned pgvector image reference}"
: "${REDIS_IMAGE_REF:?Set REDIS_IMAGE_REF to the digest-pinned Redis image reference}"
OUT_DIR="${OUT_DIR:-$ROOT/patentshield-offline-release}"
CHART_DIR="${CHART_DIR:-$ROOT/deploy/helm/patentshield}"
WHEELHOUSE_DIR="${WHEELHOUSE_DIR:-}"

if [[ -d "$OUT_DIR" && -n "$(find "$OUT_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
  echo "Output directory must be empty or absent: $OUT_DIR" >&2
  exit 2
fi
for tool in cosign sha256sum python; do
  command -v "$tool" >/dev/null || { echo "$tool is required" >&2; exit 2; }
done
[[ -r "$IMAGE_REFS_FILE" && -r "$COSIGN_PUBLIC_KEY" && -r "$COSIGN_PRIVATE_KEY" ]] || {
  echo "Image refs and both cosign keys must be readable" >&2; exit 2;
}
python - "$IMAGE_REFS_FILE" "$BACKEND_IMAGE_REF" "$FRONTEND_IMAGE_REF" "$POSTGRES_IMAGE_REF" "$REDIS_IMAGE_REF" <<'PY'
import re, sys
from pathlib import Path
refs = []
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    value = line.split("#", 1)[0].strip()
    if value:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[a-f0-9]{64}", value):
            raise SystemExit("IMAGE_REFS_FILE contains a malformed or mutable image reference")
        if value in refs:
            raise SystemExit("IMAGE_REFS_FILE contains duplicate image references")
        refs.append(value)
roles = {
    "backend": sys.argv[2],
    "frontend": sys.argv[3],
    "postgres": sys.argv[4],
    "redis": sys.argv[5],
}
for role, value in roles.items():
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[a-f0-9]{64}", value) or value not in refs:
        raise SystemExit(f"{role} image role must match an immutable reference in IMAGE_REFS_FILE")
if len(set(roles.values())) != len(roles):
    raise SystemExit("Backend, frontend, pgvector, and Redis images must be separate digest-pinned artifacts")
PY
[[ -d "$MODEL_BUNDLE_DIR" && -d "$CHART_DIR" ]] || {
  echo "Model bundle and Helm chart directories are required" >&2; exit 2;
}
[[ -f "$CHART_DIR/Chart.yaml" && -f "$CHART_DIR/values.yaml" && -f "$CHART_DIR/values.schema.json" ]] || {
  echo "Helm chart is incomplete" >&2; exit 2;
}

PYTHONPATH="$ROOT/backend${PYTHONPATH:+:$PYTHONPATH}" python -m deployment.model_bundle verify --bundle "$MODEL_BUNDLE_DIR"
mkdir -p "$OUT_DIR/oci" "$OUT_DIR/helm" "$OUT_DIR/scripts" \
  "$OUT_DIR/backend/deployment" "$OUT_DIR/deploy/airgap"
cp "$COSIGN_PUBLIC_KEY" "$OUT_DIR/cosign.pub"
cp "$IMAGE_REFS_FILE" "$OUT_DIR/image-refs.txt"
cp -a "$MODEL_BUNDLE_DIR" "$OUT_DIR/model-bundle"
cp -a "$CHART_DIR" "$OUT_DIR/helm/patentshield"
# Include a self-contained customer verifier/installer and the Compose runtime.
cp "$ROOT/docker-compose.yml" "$OUT_DIR/docker-compose.yml"
cp "$ROOT/.env.example" "$OUT_DIR/.env.example"
cp "$ROOT/deploy/airgap/nginx-compose.conf" "$OUT_DIR/deploy/airgap/nginx-compose.conf"
cp "$ROOT/deploy/airgap/README.md" "$OUT_DIR/README.md"
cp "$ROOT/deploy/airgap/customer-values.example.yaml" "$OUT_DIR/customer-values.example.yaml"
cp "$ROOT/scripts/verify_offline_release_bundle.sh" \
  "$ROOT/scripts/validate_offline_release_bundle.py" \
  "$ROOT/scripts/verify_offline_wheelhouse.py" \
  "$ROOT/scripts/install_from_offline_wheelhouse.sh" \
  "$ROOT/scripts/check_requirement_hashes.py" "$OUT_DIR/scripts/"
cp "$ROOT/backend/deployment/__init__.py" "$ROOT/backend/deployment/model_bundle.py" \
  "$OUT_DIR/backend/deployment/"
cp "$ROOT/requirements.txt" "$ROOT/requirements-ml.txt" "$ROOT/requirements-dev.txt" "$OUT_DIR/"
if [[ -n "$WHEELHOUSE_DIR" ]]; then
  [[ -d "$WHEELHOUSE_DIR" && -r "$WHEELHOUSE_DIR/SHA256SUMS" ]] || {
    echo "WHEELHOUSE_DIR must contain a SHA256SUMS manifest" >&2; exit 2;
  }
  (cd "$WHEELHOUSE_DIR" && sha256sum -c SHA256SUMS >/dev/null)
  cp -a "$WHEELHOUSE_DIR" "$OUT_DIR/wheelhouse"
fi
: > "$OUT_DIR/oci-images.tsv"

seen_refs="$OUT_DIR/.seen-image-refs"
: > "$seen_refs"
while IFS= read -r raw_ref || [[ -n "$raw_ref" ]]; do
  image_ref="${raw_ref%%#*}"
  image_ref="$(printf '%s' "$image_ref" | xargs)"
  [[ -z "$image_ref" ]] && continue
  [[ "$image_ref" =~ ^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[a-f0-9]{64}$ ]] || {
    echo "Refusing non-digest or malformed image reference: $image_ref" >&2
    exit 2
  }
  if grep -Fxq -- "$image_ref" "$seen_refs"; then
    echo "Duplicate image reference: $image_ref" >&2; exit 2
  fi
  printf '%s\n' "$image_ref" >> "$seen_refs"

  cosign verify --key "$COSIGN_PUBLIC_KEY" --insecure-ignore-tlog=true "$image_ref" >/dev/null
  safe_name="$(printf '%s' "$image_ref" | tr -c 'A-Za-z0-9._-' '_' | cut -c1-48)-$(printf '%s' "$image_ref" | sha256sum | cut -c1-12)"
  target="$OUT_DIR/oci/$safe_name"
  mkdir -p "$target"
  cosign save "$image_ref" --dir "$target"
  cosign verify --key "$COSIGN_PUBLIC_KEY" --offline --insecure-ignore-tlog=true --local-image "$target" >/dev/null
  printf '%s\t%s\n' "$image_ref" "oci/$safe_name" >> "$OUT_DIR/oci-images.tsv"
done < "$IMAGE_REFS_FILE"
[[ -s "$OUT_DIR/oci-images.tsv" ]] || { echo "No immutable OCI image refs were supplied" >&2; exit 2; }
rm -f "$seen_refs"

chart_version="$(awk '$1 == "version:" { print $2; exit }' "$CHART_DIR/Chart.yaml")"
app_version="$(awk '$1 == "appVersion:" { print $2; exit }' "$CHART_DIR/Chart.yaml" | tr -d '"')"
python - "$OUT_DIR" "$chart_version" "$app_version" "$BACKEND_IMAGE_REF" "$FRONTEND_IMAGE_REF" "$POSTGRES_IMAGE_REF" "$REDIS_IMAGE_REF" <<'PY'
import json, re, sys
from pathlib import Path
out = Path(sys.argv[1])
refs = []
for line in (out / "image-refs.txt").read_text(encoding="utf-8").splitlines():
    value = line.split("#", 1)[0].strip()
    if value and value not in refs:
        refs.append(value)
roles = {
    "backend": sys.argv[4],
    "frontend": sys.argv[5],
    "postgres": sys.argv[6],
    "redis": sys.argv[7],
}
pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[a-f0-9]{64}$")
for role, image_ref in roles.items():
    if not pattern.fullmatch(image_ref) or image_ref not in refs:
        raise SystemExit(f"{role.upper()}_IMAGE_REF must be an immutable ref present in IMAGE_REFS_FILE")
if len(set(roles.values())) != len(roles):
    raise SystemExit("Backend, frontend, pgvector, and Redis image roles must be distinct")
manifest = {
    "schema_version": 1,
    "product": "PatentShield",
    "runtime_mode": "air-gapped",
    "helm_chart": "patentshield",
    "helm_chart_version": sys.argv[2],
    "application_version": sys.argv[3],
    "image_refs": refs,
    "images": roles,
    "model_bundle_schema": 2,
    "wheelhouse_included": (out / "wheelhouse").is_dir(),
}
(out / "deployment-manifest.json").write_text(
    json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY

python "$ROOT/scripts/validate_offline_release_bundle.py" "$OUT_DIR"
(
  cd "$OUT_DIR"
  find . -type f ! -name SHA256SUMS ! -name SHA256SUMS.sigstore.json -print0 \
    | sort -z | xargs -0 sha256sum > SHA256SUMS
  sha256sum -c SHA256SUMS >/dev/null
  cosign sign-blob --key "$COSIGN_PRIVATE_KEY" --tlog-upload=false --bundle SHA256SUMS.sigstore.json SHA256SUMS
  cosign verify-blob --key "$COSIGN_PUBLIC_KEY" --insecure-ignore-tlog=true \
    --bundle SHA256SUMS.sigstore.json SHA256SUMS >/dev/null
)
printf 'Created signed air-gap release bundle: %s\n' "$OUT_DIR"
printf 'Contents: immutable OCI images, Helm chart, model bundle%s\n' \
  "$( [[ -d "$OUT_DIR/wheelhouse" ]] && printf ', offline wheelhouse' || printf ', no wheelhouse (images contain runtime dependencies)' )"
