# PatentShield air-gapped release and installation

PatentShield's air-gapped release is built on a **connected, controlled staging workstation** and transferred into the isolated customer zone. Runtime containers and pods do not need public internet access. The release contains digest-pinned backend, frontend, pgvector, and Redis OCI images, the exact Helm chart, signed Compose/Helm operator files, a signed three-model bundle, and optionally a target-compatible Python wheelhouse.

> Offline mode deliberately disables external search, translation-provider calls, and external surveillance/connector sync. Those network-dependent capabilities must remain disabled unless the customer provides an explicitly reviewed private service endpoint and changes the network policy.

## 1. Release inputs and trust model

Keep these outside source control and outside the transfer ZIP: Cosign private keys, customer runtime secrets, database/KMS keys, S3 credentials, TLS private keys, model weights, registry credentials, and production image digests.

The release uses:

- One immutable backend image reference for Django API, Celery workers, and Beat.
- A separate immutable frontend image reference for the React SPA.
- Digest-pinned pgvector and Redis images for the single-host Compose runtime; Kubernetes customers may provision managed services but the signed transfer format still validates four image roles.
- A Cosign public key delivered with the bundle, whose SHA-256 fingerprint must be checked through a separate trusted channel before use.
- A three-model bundle: BGE-M3, BGE reranker v2 M3, and the FastEmbed BGE-large ONNX model used by public-patent semantic search.
- A Helm chart with default-deny network policy, restricted egress routes, read-only model mounts, non-root containers, and digest-only image references.

The bundle signature proves integrity against the release signing key; it does not prove that the key itself is trusted. Validate the public-key fingerprint out of band.

## 2. Build hash-locked dependencies

Run the following on the connected release workstation after reviewing dependency changes:

```bash
scripts/compile_requirements.sh
python scripts/verify_dependency_manifest.py --require-hashes
```

The Docker build uses `pip install --require-hashes`; it intentionally fails closed for any dependency without hashes. The current `requirements-ml.txt` includes `FlagEmbedding==1.4.2` without a SHA-256 hash, so **the ML-enabled backend image and ML wheelhouse remain blocked until the ML lock is regenerated and reviewed**. Do not bypass the hash check or downgrade to `--no-require-hashes`.

Build the backend image with the ML stack included:

```bash
docker buildx build --platform linux/amd64 \
  --build-arg INSTALL_ML_DEPS=1 \
  --tag registry.internal.example/patentshield-api:<release> \
  --push .
```

The root `Dockerfile` pins its Python base by digest. It installs the hash-locked base and ML requirements in a builder stage, then copies the virtual environment into a non-root runtime image.

Build the React frontend separately. Both base images **must** be supplied as immutable `name:tag@sha256:<digest>` references; floating tags are not accepted by the release process:

```bash
docker buildx build --platform linux/amd64 \
  --file frontend/Dockerfile \
  --build-arg NODE_BUILD_BASE='node:<approved-version>@sha256:<approved-node-digest>' \
  --build-arg NGINX_RUNTIME_BASE='nginxinc/nginx-unprivileged:<approved-version>@sha256:<approved-nginx-digest>' \
  --build-arg VITE_API_BASE_URL=/ \
  --build-arg VITE_WS_BASE_URL=/ \
  --tag registry.internal.example/patentshield-frontend:<release> \
  --push .
```

The frontend image runs as UID/GID 101, serves only the Vite build output on port 8080, supports SPA route fallback, and exposes `/health/live`. Ingress routes `/api`, `/ws`, `/health`, `/admin`, `/static`, and `/media` to Django and the root SPA paths to the frontend service.

Mirror the approved pgvector and Redis images into the internal registry as well. Sign all four mirrored images with the same release key used by the exporter. Record their immutable digest references (not tags) in `image-refs.txt`, and set `BACKEND_IMAGE_REF`, `FRONTEND_IMAGE_REF`, `POSTGRES_IMAGE_REF`, and `REDIS_IMAGE_REF` to the exact corresponding lines. The API, worker, and Beat can share the backend image digest.

For a single-host Docker Compose installation, first install the verified model bundle into the local model directory:

```bash
PYTHONPATH=backend python -m deployment.model_bundle install \
  --bundle /transfer/model-bundle --target ./offline/installed-models
```

Set `PATENTSHIELD_INSTALLED_MODELS_DIR=./offline/installed-models` and `PATENTSHIELD_OFFLINE_MODE=True` in the repository `.env`. Set `PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_LOCAL_FILES_ONLY=True` and copy the exact BGE-M3, reranker, and public-patent model commit IDs from the installed `patentshield-model-bundle-manifest.json` into `PATENTSHIELD_BGE_MODEL_REVISION`, `PATENTSHIELD_RERANKER_MODEL_REVISION`, and `PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_REVISION`. On offline startup, each adapter verifies the model ID, immutable revision, complete file inventory, file sizes, and SHA-256 hashes before loading local weights. The API, regular worker, and embedding worker mount this directory read-only. Do not point the model-cache setting at a writable or externally populated cache in offline mode.

The Compose file is a production-style offline runtime: it uses only pre-imported digest-pinned images (`PATENTSHIELD_BACKEND_IMAGE`, `PATENTSHIELD_FRONTEND_IMAGE`, `PATENTSHIELD_POSTGRES_IMAGE`, and `PATENTSHIELD_REDIS_IMAGE`), `pull_policy: never`, and no host source-code mounts. The frontend gateway exposes a single loopback HTTP endpoint on port 8080 and proxies `/api`, `/ws`, `/health`, `/admin`, `/static`, and `/media` to the API container. Put a customer-managed TLS reverse proxy in front of that loopback endpoint for production use. The PostgreSQL and Redis images must also be included in `image-refs.txt`, mirrored, and signed with the release key if you use the bundled Compose deployment.

## 3. Export exact model snapshots

Resolve and review an immutable commit ID for each repository. Do not use `main`, a branch, or a mutable tag. The exporter requires lowercase 40- or 64-character hexadecimal commit IDs.

On the connected staging workstation, materialize these snapshots into separate directories:

```python
import os
from huggingface_hub import snapshot_download

models = [
    ("BAAI/bge-m3", os.environ["BGE_M3_REVISION"], "staging-models/bge-m3"),
    ("BAAI/bge-reranker-v2-m3", os.environ["BGE_RERANKER_REVISION"], "staging-models/reranker"),
    ("qdrant/bge-large-en-v1.5-onnx", os.environ["PUBLIC_PATENT_REVISION"], "staging-models/public-patent-embedding"),
]
for repo, revision, destination in models:
    snapshot_download(repo_id=repo, revision=revision, local_dir=destination)
```

Export and verify the model bundle:

```bash
PYTHONPATH=backend python -m deployment.model_bundle export \
  --bge-source staging-models/bge-m3 --bge-revision "$BGE_M3_REVISION" \
  --reranker-source staging-models/reranker --reranker-revision "$BGE_RERANKER_REVISION" \
  --public-patent-source staging-models/public-patent-embedding \
  --public-patent-revision "$PUBLIC_PATENT_REVISION" \
  --output staging-model-bundle

PYTHONPATH=backend python -m deployment.model_bundle verify --bundle staging-model-bundle
```

The bundle contains SHA-256 and byte-size inventory for every model file. The public-patent model is installed in the Hugging Face cache layout used by FastEmbed. The install command also writes `patentshield-model-bundle-manifest.json` beside the installed model directories. In offline mode, all three model adapters compare their model ID and immutable revision with runtime settings and verify the complete installed file inventory, sizes, and SHA-256 hashes before loading weights.

## 4. Optional offline Python wheelhouse

Use a wheelhouse only when the isolated environment needs to install Python dependencies outside the signed application image. It must target the exact Python version, implementation, operating system, architecture, and platform tag used by the target image.

```bash
INCLUDE_ML_DEPS=1 WHEELHOUSE_DIR=staging-wheelhouse scripts/build_offline_wheelhouse.sh
```

This command currently fails closed because `FlagEmbedding` is not hash-locked. After the ML lock is regenerated and approved, create and transfer the wheelhouse. Install it in the matching runtime environment with:

```bash
WHEELHOUSE_DIR=/transfer/wheelhouse scripts/install_from_offline_wheelhouse.sh requirements-ml.txt
```

The installer verifies the signed file checksums, exact lockfile hash, runtime platform metadata, and artifact inventory before `pip install --no-index --require-hashes`.

## 5. Create and sign the transfer bundle

Sign the backend, frontend, pgvector, and Redis images in the connected staging environment using your organization's release key. Then export:

```bash
export IMAGE_REFS_FILE=/secure/release-workspace/image-refs.txt
export BACKEND_IMAGE_REF='registry.internal.example/patentshield-api@sha256:<64-hex-digest>'
export FRONTEND_IMAGE_REF='registry.internal.example/patentshield-frontend@sha256:<64-hex-digest>'
export POSTGRES_IMAGE_REF='registry.internal.example/pgvector@sha256:<64-hex-digest>'
export REDIS_IMAGE_REF='registry.internal.example/redis@sha256:<64-hex-digest>'
export COSIGN_PUBLIC_KEY=/secure/keys/cosign.pub
export COSIGN_PRIVATE_KEY=/secure/keys/cosign.key
export MODEL_BUNDLE_DIR=/secure/release-workspace/staging-model-bundle
export CHART_DIR="$PWD/deploy/helm/patentshield"
export OUT_DIR=/secure/release-workspace/patentshield-offline-release
# Optional, only after the ML lock and wheelhouse are hash-verified:
# export WHEELHOUSE_DIR=/secure/release-workspace/staging-wheelhouse

scripts/export_signed_oci_bundle.sh
```

The script verifies every registry signature, saves OCI layouts locally, verifies the saved layouts, and copies the exact chart, model bundle, Compose gateway, customer values, verification scripts, and dependency locks. It records backend/frontend/pgvector/Redis image roles in `deployment-manifest.json`, creates `SHA256SUMS`, and signs the checksum manifest with Cosign.

## 6. Verify before importing in the isolated zone

Transfer the bundle through the approved media gateway. Before trusting the included public key, compare its fingerprint with a value received through a separate trusted channel. Then run:

```bash
cd /transfer/patentshield-offline-release
COSIGN_PUBLIC_KEY="$PWD/cosign.pub" scripts/verify_offline_release_bundle.sh "$PWD"
```

This verifies the signed checksum manifest, every bundled file, the bundle structure, the three-model manifest and file hashes, and each local OCI image signature. Do not load an image or install the chart before verification succeeds.

Import the verified OCI images into the customer's private registry or directly into every Kubernetes node's container runtime using the organization's approved OCI-copy/import process. The images must remain addressable by the exact digest values recorded in the customer Helm values file or Compose `.env`.

For the single-host Compose runtime, create `.env` from `.env.example`, set the four `PATENTSHIELD_*_IMAGE` values to the imported digest references, set customer database credentials and model revisions, then verify and start without building or pulling:

```bash
cp .env.example .env
# Edit .env using the verified image refs and your secret manager's runtime values.
docker compose config
docker compose up -d --no-build
```

Compose uses `pull_policy: never`, and the transferred release includes the exact Nginx same-origin gateway config. Only port 8080 is published on loopback; expose it through an approved TLS reverse proxy.

## 7. Provision storage and install

Provision these customer-managed resources before deployment:

- PostgreSQL with the required pgvector extension and a reviewed backup/restore process.
- Redis for Channels and Celery.
- A model-bundle PVC containing the verified `model-bundle` directory and a model PVC writable by the one-time installer and readable by API/worker pods.
- Either a customer-encrypted media PVC (`storage.backend=filesystem`) or a private S3-compatible endpoint configured for TLS and SSE-KMS (`storage.backend=s3`).
- A Kubernetes Secret named by `runtimeSecret` containing the application's secrets, database/broker URLs, storage credentials, encryption keys, and the exact three model revision IDs copied from `model-bundle/manifest.json`.
- Ingress TLS secret and internal registry pull secret where required.

The installer job is optional because many customers pre-populate the model PVC. To use it, set `modelBundle.installJob.enabled=true`; ensure the bundle and model PVCs are mounted and wait for the installer Job to complete before treating model-dependent API/worker operations as ready. The model PVC must support the access mode needed by the installer and the multiple read-only API/worker mounts; provision RWX where required by the storage platform.

Copy `customer-values.example.yaml`, replace both image repositories and digests, customer host/TLS/registry names, storage/network selectors and PVC names, then install the chart:

```bash
helm lint deploy/helm/patentshield -f /secure/customer-values.yaml
helm upgrade --install patentshield deploy/helm/patentshield \
  --namespace patentshield --create-namespace \
  -f /secure/customer-values.yaml
```

The chart rejects mutable or malformed backend/frontend image references and fails closed if S3 storage is paired with a default-deny network policy but no private object-storage egress route is configured. Review the generated NetworkPolicies against the customer's real namespace, pod labels, DB, Redis and object-store endpoints before rollout.

## 8. Operations and recovery

- Keep public internet egress denied. Do not enable public translation/search/surveillance providers in an air-gapped installation.
- Retain signing keys and key fingerprints separately from transfer media.
- Use the provided `scripts/backup_database.sh` and `scripts/restore_database.sh` with age-encrypted backup files and a reviewed key-management process.
- Perform a restore rehearsal before accepting production data.
- Keep model bundle revisions and the embedding revision values identical; changing an embedding model requires the documented reindex/migration procedure.
- Re-verify every transferred release after copying it to a new medium or registry.
- Plan a controlled update window for image, chart, model and dependency-bundle replacement; never overwrite customer data or PVCs as part of an application image update.

## Known release gate

The current source tree's `requirements-ml.txt` has one pinned package (`FlagEmbedding==1.4.2`) without a SHA-256 hash. This phase intentionally makes hash enforcement mandatory, so ML-enabled image construction and ML wheelhouse generation will stop until `scripts/compile_requirements.sh` is run on a connected reviewed build machine and the regenerated lock is committed. No placeholder hash is supplied here.
