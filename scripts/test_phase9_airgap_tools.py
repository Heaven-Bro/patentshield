"""Regression tests for Phase 9 air-gapped release validators."""
from __future__ import annotations

import hashlib
import json
import platform
import sysconfig
import tempfile
import unittest
from pathlib import Path

from check_requirement_hashes import find_unhashed
from validate_offline_release_bundle import validate
from verify_offline_wheelhouse import verify as verify_wheelhouse

IMAGE_REF = "registry.internal.example/patentshield@sha256:" + "a" * 64


class RequirementHashCheckerTests(unittest.TestCase):
    def test_detects_pinned_package_without_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "requirements.txt"
            path.write_text("safe-package==1.2.3 \\\n  --hash=sha256:" + "a" * 64 + "\nunhashed-package==4.5.6\n", encoding="utf-8")
            self.assertEqual(find_unhashed(path), ["unhashed-package"])

    def test_comments_and_non_pin_options_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "requirements.txt"
            path.write_text("# example==1\n--index-url https://example.invalid/simple\n", encoding="utf-8")
            self.assertEqual(find_unhashed(path), [])


class OfflineReleaseValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.refs = {
            "backend": IMAGE_REF,
            "frontend": "registry.internal.example/patentshield-frontend@sha256:" + "d" * 64,
            "postgres": "registry.internal.example/pgvector@sha256:" + "e" * 64,
            "redis": "registry.internal.example/redis@sha256:" + "f" * 64,
        }
        (self.root / "image-refs.txt").write_text(
            "\n".join(self.refs.values()) + "\n", encoding="utf-8"
        )
        mapping = []
        for role, image_ref in self.refs.items():
            layout_rel = f"oci/{role}"
            layout = self.root / layout_rel
            layout.mkdir(parents=True)
            (layout / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}\n', encoding="utf-8")
            (layout / "index.json").write_text(json.dumps({
                "schemaVersion": 2,
                "manifests": [{
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": "sha256:" + ("b" if role == "backend" else "c") * 64,
                    "size": 10,
                }],
            }), encoding="utf-8")
            mapping.append(f"{image_ref}\t{layout_rel}")
        (self.root / "oci-images.tsv").write_text("\n".join(mapping) + "\n", encoding="utf-8")
        for relative in (
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
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n", encoding="utf-8")
        model_bundle = self.root / "model-bundle"
        model_bundle.mkdir()
        (model_bundle / "manifest.json").write_text(json.dumps({
            "schema_version": 2,
            "models": {
                "bge-m3": {},
                "reranker": {},
                "public-patent-embedding": {},
            },
        }), encoding="utf-8")
        (self.root / "deployment-manifest.json").write_text(json.dumps({
            "schema_version": 1,
            "image_refs": list(self.refs.values()),
            "images": self.refs,
            "wheelhouse_included": False,
        }), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_valid_release_structure_passes(self) -> None:
        self.assertEqual(validate(self.root), sorted(self.refs.values()))

    def test_path_traversal_layout_is_rejected(self) -> None:
        first = next(iter(self.refs.values()))
        (self.root / "oci-images.tsv").write_text(f"{first}\toci/../escape\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            validate(self.root)

    def test_image_inventory_mismatch_is_rejected(self) -> None:
        (self.root / "image-refs.txt").write_text(
            "\n".join(self.refs.values()) + "\nregistry.invalid/extra@sha256:" + "a" * 64 + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            validate(self.root)

    def test_empty_oci_index_is_rejected(self) -> None:
        (self.root / "oci/backend" / "index.json").write_text(
            '{"schemaVersion":2,"manifests":[]}', encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            validate(self.root)

    def test_missing_compose_gateway_is_rejected(self) -> None:
        (self.root / "deploy/airgap/nginx-compose.conf").unlink()
        with self.assertRaises(FileNotFoundError):
            validate(self.root)

    def test_image_roles_must_be_distinct(self) -> None:
        manifest_path = self.root / "deployment-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["images"]["redis"] = manifest["images"]["postgres"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ValueError):
            validate(self.root)

    def test_symlinked_wheelhouse_is_rejected(self) -> None:
        outside = self.root.parent / (self.root.name + "-outside")
        outside.mkdir(exist_ok=True)
        (self.root / "wheelhouse").symlink_to(outside, target_is_directory=True)
        try:
            with self.assertRaises(ValueError):
                validate(self.root)
        finally:
            (self.root / "wheelhouse").unlink()
            outside.rmdir()

    def test_manifest_wheelhouse_flag_must_match_contents(self) -> None:
        (self.root / "wheelhouse").mkdir()
        (self.root / "wheelhouse" / "SHA256SUMS").write_text("", encoding="utf-8")
        with self.assertRaises(ValueError):
            validate(self.root)


class OfflineWheelhouseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.wheelhouse = self.root / "wheelhouse"
        self.wheelhouse.mkdir()
        self.lock = self.root / "requirements.txt"
        self.lock.write_text("example==1.0 --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8")
        wheel = self.wheelhouse / "example-1.0-py3-none-any.whl"
        wheel.write_bytes(b"wheel-fixture")
        self.manifest = {
            "schema_version": 1,
            "python_version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "system": platform.system(),
            "machine": platform.machine(),
            "platform_tag": sysconfig.get_platform(),
            "requirements": [{"file": "requirements.txt", "sha256": hashlib.sha256(self.lock.read_bytes()).hexdigest()}],
            "artifacts": [wheel.name],
        }
        (self.wheelhouse / "wheelhouse-manifest.json").write_text(
            json.dumps(self.manifest), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_exact_lock_and_platform_are_required(self) -> None:
        verify_wheelhouse(self.wheelhouse, self.lock)

    def test_changed_lock_is_rejected(self) -> None:
        self.lock.write_text("example==1.1 --hash=sha256:" + "b" * 64 + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            verify_wheelhouse(self.wheelhouse, self.lock)

    def test_platform_mismatch_is_rejected(self) -> None:
        self.manifest["platform_tag"] = "foreign-platform"
        (self.wheelhouse / "wheelhouse-manifest.json").write_text(
            json.dumps(self.manifest), encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            verify_wheelhouse(self.wheelhouse, self.lock)


class AirGappedRuntimeSourceContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]

    def test_compose_uses_digest_variables_without_source_mounts(self) -> None:
        compose = (self.root / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("pull_policy: never", compose)
        self.assertNotIn("./backend:/app", compose)
        self.assertNotIn("./frontend:/frontend", compose)
        for variable in (
            "PATENTSHIELD_BACKEND_IMAGE",
            "PATENTSHIELD_FRONTEND_IMAGE",
            "PATENTSHIELD_POSTGRES_IMAGE",
            "PATENTSHIELD_REDIS_IMAGE",
        ):
            self.assertIn("${" + variable + ":?", compose)
        for flag in (
            "PATENTSHIELD_OFFLINE_MODE: \"true\"",
            "HF_HUB_OFFLINE: \"1\"",
            "TRANSFORMERS_OFFLINE: \"1\"",
        ):
            self.assertIn(flag, compose)

    def test_model_revision_examples_are_explicit_placeholders(self) -> None:
        env_example = (self.root / ".env.example").read_text(encoding="utf-8")
        for key in (
            "PATENTSHIELD_BGE_MODEL_REVISION",
            "PATENTSHIELD_RERANKER_MODEL_REVISION",
            "PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_REVISION",
        ):
            line = next(line for line in env_example.splitlines() if line.startswith(key + "="))
            self.assertIn("REPLACE_WITH_IMMUTABLE_40_OR_64_HEX_COMMIT_ID", line)
            self.assertNotEqual(line.split("=", 1)[1].split()[0], "main")

    def test_backend_image_collects_static_assets_before_non_root_runtime(self) -> None:
        dockerfile = (self.root / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("python manage.py collectstatic --noinput", dockerfile)
        self.assertLess(dockerfile.index("collectstatic --noinput"), dockerfile.index("USER django"))

    def test_frontend_gateway_routes_api_and_websockets_to_backend(self) -> None:
        gateway = (self.root / "deploy/airgap/nginx-compose.conf").read_text(encoding="utf-8")
        self.assertIn("location ^~ /api/", gateway)
        self.assertIn("location ^~ /ws/", gateway)
        self.assertIn("proxy_pass http://api:8000", gateway)
        self.assertIn("try_files $uri $uri/ /index.html", gateway)


if __name__ == "__main__":
    unittest.main()
