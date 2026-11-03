#!/usr/bin/env python3
"""Validate signed air-gap bundle layout and prevent unsafe OCI path references."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path, PurePosixPath

IMAGE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[a-f0-9]{64}$")


def safe_bundle_path(bundle: Path, raw: str) -> Path:
    pure = PurePosixPath(raw)
    if not raw or pure.is_absolute() or ".." in pure.parts or "\\" in raw or pure.as_posix() != raw:
        raise ValueError(f"Unsafe bundle path: {raw!r}")
    target = bundle.joinpath(*pure.parts)
    cursor = bundle
    for part in pure.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"Symlink is not allowed in release bundle: {raw}")
    resolved = target.resolve(strict=True)
    if not resolved.is_relative_to(bundle.resolve()):
        raise ValueError(f"Path escapes release bundle: {raw}")
    return resolved


def read_image_refs(path: Path) -> list[str]:
    refs: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        value = line.split("#", 1)[0].strip()
        if not value:
            continue
        if not IMAGE_REF.fullmatch(value):
            raise ValueError(f"Malformed immutable OCI image reference at line {number}")
        if value in refs:
            raise ValueError(f"Duplicate OCI image reference: {value}")
        refs.append(value)
    if not refs:
        raise ValueError("Offline bundle contains no OCI image references")
    return refs


def validate(bundle: Path) -> list[str]:
    if bundle.is_symlink():
        raise ValueError("Offline release bundle root must not be a symlink")
    bundle = bundle.resolve(strict=True)
    refs_file = safe_bundle_path(bundle, "image-refs.txt")
    table_file = safe_bundle_path(bundle, "oci-images.tsv")
    refs = read_image_refs(refs_file)
    mappings: dict[str, str] = {}
    for line_number, line in enumerate(table_file.read_text(encoding="utf-8").splitlines(), 1):
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != 2:
            raise ValueError(f"Malformed OCI mapping at line {line_number}")
        image_ref, layout_path = fields
        if not IMAGE_REF.fullmatch(image_ref) or image_ref in mappings:
            raise ValueError(f"Invalid or duplicate OCI mapping at line {line_number}")
        if not layout_path.startswith("oci/"):
            raise ValueError(f"OCI layout must live under oci/: {layout_path}")
        layout = safe_bundle_path(bundle, layout_path)
        if not layout.is_dir():
            raise ValueError(f"OCI layout directory missing: {layout_path}")
        oci_layout = safe_bundle_path(bundle, f"{layout_path}/oci-layout")
        index_path = safe_bundle_path(bundle, f"{layout_path}/index.json")
        try:
            layout_doc = json.loads(oci_layout.read_text(encoding="utf-8"))
            index_doc = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid OCI layout metadata: {layout_path}") from exc
        if not isinstance(layout_doc, dict) or layout_doc.get("imageLayoutVersion") != "1.0.0":
            raise ValueError(f"Unsupported OCI layout version: {layout_path}")
        if not isinstance(index_doc, dict):
            raise ValueError(f"Invalid OCI index.json object: {layout_path}")
        descriptors = index_doc.get("manifests")
        if index_doc.get("schemaVersion") != 2 or not isinstance(descriptors, list) or not descriptors:
            raise ValueError(f"Invalid or empty OCI index.json: {layout_path}")
        for descriptor in descriptors:
            if not isinstance(descriptor, dict):
                raise ValueError(f"Malformed OCI descriptor: {layout_path}")
            digest = descriptor.get("digest")
            size = descriptor.get("size")
            if not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
                raise ValueError(f"Malformed OCI descriptor digest: {layout_path}")
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise ValueError(f"Malformed OCI descriptor size: {layout_path}")
        if layout_path in mappings.values():
            raise ValueError(f"OCI layout is assigned to more than one image reference: {layout_path}")
        mappings[image_ref] = layout_path
    if set(mappings) != set(refs):
        raise ValueError("image-refs.txt and oci-images.tsv do not contain the same image reference set")

    chart_required = (
        "helm/patentshield/Chart.yaml",
        "helm/patentshield/values.yaml",
        "helm/patentshield/values.schema.json",
        "helm/patentshield/templates/api-deployment.yaml",
        "helm/patentshield/templates/worker-deployment.yaml",
        "docker-compose.yml",
        ".env.example",
        "deploy/airgap/nginx-compose.conf",
        "README.md",
        "customer-values.example.yaml",
        "scripts/verify_offline_release_bundle.sh",
        "scripts/validate_offline_release_bundle.py",
        "scripts/verify_offline_wheelhouse.py",
        "scripts/install_from_offline_wheelhouse.sh",
        "scripts/check_requirement_hashes.py",
        "backend/deployment/__init__.py",
        "backend/deployment/model_bundle.py",
        "requirements.txt",
        "requirements-ml.txt",
        "requirements-dev.txt",
    )
    for relative in chart_required:
        path = safe_bundle_path(bundle, relative)
        if not path.is_file():
            raise ValueError(f"Required Helm chart file missing: {relative}")

    manifest_path = safe_bundle_path(bundle, "deployment-manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("deployment-manifest.json is invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("Unsupported deployment-manifest.json schema")
    manifest_refs = manifest.get("image_refs")
    if not isinstance(manifest_refs, list) or any(not isinstance(item, str) for item in manifest_refs):
        raise ValueError("Deployment manifest image_refs must be a string array")
    if len(manifest_refs) != len(set(manifest_refs)) or set(manifest_refs) != set(refs):
        raise ValueError("Deployment manifest image references disagree with the OCI inventory")
    image_roles = manifest.get("images")
    required_image_roles = {"backend", "frontend", "postgres", "redis"}
    if not isinstance(image_roles, dict) or set(image_roles) != required_image_roles:
        raise ValueError("Deployment manifest must identify backend, frontend, pgvector, and Redis image references")
    if any(not isinstance(value, str) or value not in refs for value in image_roles.values()):
        raise ValueError("All deployment image roles must reference images in the signed OCI inventory")
    if len(set(image_roles.values())) != len(required_image_roles):
        raise ValueError("Backend, frontend, pgvector, and Redis must have separate digest-pinned images")

    model_bundle = safe_bundle_path(bundle, "model-bundle")
    model_manifest_path = safe_bundle_path(bundle, "model-bundle/manifest.json")
    if not model_bundle.is_dir() or not model_manifest_path.is_file():
        raise ValueError("Offline model bundle is missing")
    try:
        model_manifest = json.loads(model_manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Offline model bundle manifest is invalid") from exc
    if (
        not isinstance(model_manifest, dict)
        or model_manifest.get("schema_version") != 2
        or not isinstance(model_manifest.get("models"), dict)
        or set(model_manifest["models"]) != {"bge-m3", "reranker", "public-patent-embedding"}
    ):
        raise ValueError("Offline model bundle manifest does not contain the three supported model families")

    wheelhouse = bundle / "wheelhouse"
    wheelhouse_present = wheelhouse.exists() or wheelhouse.is_symlink()
    if wheelhouse_present:
        wheelhouse_path = safe_bundle_path(bundle, "wheelhouse")
        if not wheelhouse_path.is_dir():
            raise ValueError("Optional wheelhouse must be a regular directory")
        for item in wheelhouse_path.rglob("*"):
            if item.is_symlink():
                raise ValueError("Symlinks are not allowed in the offline wheelhouse")
        checksums = safe_bundle_path(bundle, "wheelhouse/SHA256SUMS")
        if not checksums.is_file():
            raise ValueError("Optional wheelhouse is incomplete or unsafe")
    if not isinstance(manifest.get("wheelhouse_included"), bool):
        raise ValueError("Deployment manifest wheelhouse_included must be a boolean")
    if manifest["wheelhouse_included"] != wheelhouse_present:
        raise ValueError("Deployment manifest wheelhouse_included flag disagrees with bundle contents")

    return sorted(refs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    try:
        refs = validate(args.bundle)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Offline release bundle validation failed: {exc}", file=sys.stderr)
        return 1
    print(f"Offline release bundle structure validated ({len(refs)} immutable OCI images).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
