# Air-gap release staging

This folder contains instructions and a placeholder image-ref file only. It deliberately contains no model weights, registry credentials, KMS material, signing key, signed customer image digest, or secret-bearing Kubernetes manifest.

Create `image-refs.txt` in the controlled release workspace (or copy `image-refs.example.txt`) with immutable `repository@sha256:<digest>` references for every API/worker/beat image. Use `scripts/sign_release_images.sh` with the release key, then `scripts/export_signed_oci_bundle.sh` to prepare a transfer package. In the isolated zone, run `scripts/verify_offline_release_bundle.sh` before loading any images or installing the chart.

The offline package includes a signed `SHA256SUMS` manifest, Cosign public key, model bundle manifest and files, OCI layouts, and source image refs. Validate the public key fingerprint through a separate trusted channel; do not trust a key solely because it arrived beside the data it verifies.
