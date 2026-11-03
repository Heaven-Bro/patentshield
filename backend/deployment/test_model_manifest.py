"""Offline model manifest verification tests; no Django or model runtime required."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from deployment.model_manifest import ModelManifestError, verify_installed_model


REVISION = "a" * 40


class InstalledModelManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.model_dir = self.root / "bge-m3"
        self.model_dir.mkdir()
        self.weight = self.model_dir / "model.safetensors"
        self.weight.write_bytes(b"sample model weights for checksum verification")
        self.manifest_path = self.root / "patentshield-model-bundle-manifest.json"
        self._write_manifest()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _record(self, model_id: str, revision: str, model_file: Path) -> dict:
        data = model_file.read_bytes()
        return {
            "model_id": model_id,
            "revision": revision,
            "format": "transformers",
            "path": "models/bge-m3",
            "files": [{
                "path": model_file.name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }],
        }

    def _write_manifest(self, *, bge_record: dict | None = None) -> None:
        placeholder = self._record("BAAI/bge-m3", REVISION, self.weight)
        self.manifest_path.write_text(json.dumps({
            "schema_version": 2,
            "models": {
                "bge-m3": bge_record or placeholder,
                "reranker": {
                    "model_id": "BAAI/bge-reranker-v2-m3",
                    "revision": "b" * 40,
                    "files": [{"path": "weights.safetensors", "sha256": "c" * 64, "size": 1}],
                },
                "public-patent-embedding": {
                    "model_id": "BAAI/bge-large-en-v1.5",
                    "revision": "d" * 40,
                    "files": [{"path": "model.onnx", "sha256": "e" * 64, "size": 1}],
                },
            },
        }), encoding="utf-8")

    def _verify(self) -> dict:
        return verify_installed_model(
            self.manifest_path,
            model_key="bge-m3",
            expected_model_id="BAAI/bge-m3",
            expected_revision=REVISION,
            installed_model_dir=self.model_dir,
        )

    def test_accepts_exact_model_identity_and_file_inventory(self) -> None:
        record = self._verify()
        self.assertEqual(record["model_id"], "BAAI/bge-m3")

    def test_rejects_revision_mismatch(self) -> None:
        with self.assertRaisesRegex(ModelManifestError, "revision does not match"):
            verify_installed_model(
                self.manifest_path,
                model_key="bge-m3",
                expected_model_id="BAAI/bge-m3",
                expected_revision="f" * 40,
                installed_model_dir=self.model_dir,
            )

    def test_rejects_model_file_tampering(self) -> None:
        self.weight.write_bytes(b"tampered model weights")
        with self.assertRaisesRegex(ModelManifestError, "file inventory differs|failed SHA-256"):
            self._verify()

    def test_rejects_symlink_inside_model_directory(self) -> None:
        target = self.root / "external.bin"
        target.write_bytes(b"external")
        link = self.model_dir / "external.bin"
        link.symlink_to(target)
        with self.assertRaisesRegex(ModelManifestError, "symbolic link"):
            self._verify()

    def test_rejects_manifest_path_traversal(self) -> None:
        bad = self._record("BAAI/bge-m3", REVISION, self.weight)
        bad["files"][0]["path"] = "../outside"
        self._write_manifest(bge_record=bad)
        with self.assertRaisesRegex(ModelManifestError, "unsafe file path"):
            self._verify()

    def test_rejects_missing_bundle_manifest(self) -> None:
        self.manifest_path.unlink()
        with self.assertRaisesRegex(ModelManifestError, "missing or unsafe"):
            self._verify()


if __name__ == "__main__":
    unittest.main()
