"""Standard-library tests for offline model bundle integrity and traversal defenses."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from deployment.model_bundle import BundleError, export_bundle, install_command, verify_bundle
import argparse


class OfflineModelBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bge = self.root / "source-bge"
        self.reranker = self.root / "source-reranker"
        for path in (self.bge, self.reranker):
            path.mkdir()
            (path / "config.json").write_text('{"model_type":"test"}', encoding="utf-8")
            (path / "tokenizer.json").write_text('{"version":"1.0"}', encoding="utf-8")
            (path / "model.safetensors").write_bytes(b"test-model-weights")
        self.bundle = self.root / "bundle"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _export(self) -> None:
        args = argparse.Namespace(
            bge_source=str(self.bge),
            bge_revision="test-bge-revision",
            reranker_source=str(self.reranker),
            reranker_revision="test-reranker-revision",
            output=str(self.bundle),
        )
        export_bundle(args)

    def test_export_and_verify_round_trip(self) -> None:
        self._export()
        manifest = verify_bundle(self.bundle)
        self.assertEqual(set(manifest["models"]), {"bge-m3", "reranker"})
        self.assertEqual(manifest["models"]["bge-m3"]["revision"], "test-bge-revision")

    def test_tampered_file_is_rejected(self) -> None:
        self._export()
        target = self.bundle / "models" / "bge-m3" / "model.safetensors"
        target.write_bytes(b"tampered")
        with self.assertRaises(BundleError):
            verify_bundle(self.bundle)

    def test_path_traversal_in_manifest_is_rejected(self) -> None:
        self._export()
        manifest_path = self.bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["models"]["bge-m3"]["path"] = "../escape"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(BundleError):
            verify_bundle(self.bundle)

    def test_symlinks_are_rejected(self) -> None:
        self._export()
        link = self.bundle / "models" / "bge-m3" / "external-link"
        link.symlink_to(self.reranker / "config.json")
        with self.assertRaises(BundleError):
            verify_bundle(self.bundle)


    def test_install_copies_verified_models_and_refuses_implicit_overwrite(self) -> None:
        self._export()
        target = self.root / "installed"
        install_args = argparse.Namespace(
            bundle=str(self.bundle), target=str(target), replace=False
        )
        self.assertEqual(install_command(install_args), 0)
        self.assertTrue((target / "bge-m3" / "model.safetensors").is_file())
        self.assertTrue((target / "reranker" / "tokenizer.json").is_file())
        with self.assertRaises(BundleError):
            install_command(install_args)

    def test_missing_weight_files_are_rejected(self) -> None:
        (self.bge / "model.safetensors").unlink()
        with self.assertRaises(BundleError):
            self._export()


if __name__ == "__main__":
    unittest.main()
