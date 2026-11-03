"""Standard-library tests for offline model bundle integrity and traversal defenses."""
from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path

from deployment.model_bundle import BundleError, export_bundle, install_command, verify_bundle

REV_BGE = "1" * 40
REV_RERANKER = "2" * 40
REV_PUBLIC = "3" * 40


class OfflineModelBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bge = self.root / "source-bge"
        self.reranker = self.root / "source-reranker"
        self.public = self.root / "source-public-patent"
        self._transformer_fixture(self.bge)
        self._transformer_fixture(self.reranker)
        self._fastembed_fixture(self.public)
        self.bundle = self.root / "bundle"

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _transformer_fixture(path: Path) -> None:
        path.mkdir()
        (path / "config.json").write_text(
            json.dumps({"model_type": "test", "hidden_size": 1024}), encoding="utf-8"
        )
        (path / "tokenizer.json").write_text('{"version":"1.0"}', encoding="utf-8")
        (path / "model.safetensors").write_bytes(b"test-model-weights")

    @staticmethod
    def _fastembed_fixture(path: Path) -> None:
        path.mkdir()
        (path / "config.json").write_text(
            json.dumps({"model_type": "bert", "hidden_size": 1024}), encoding="utf-8"
        )
        (path / "tokenizer.json").write_text('{"version":"1.0"}', encoding="utf-8")
        (path / "tokenizer_config.json").write_text('{"do_lower_case":true}', encoding="utf-8")
        (path / "special_tokens_map.json").write_text('{"pad_token":"[PAD]"}', encoding="utf-8")
        (path / "model.onnx").write_bytes(b"test-onnx-model-weights")

    def _export(self) -> None:
        args = argparse.Namespace(
            bge_source=str(self.bge),
            bge_revision=REV_BGE,
            reranker_source=str(self.reranker),
            reranker_revision=REV_RERANKER,
            public_patent_source=str(self.public),
            public_patent_revision=REV_PUBLIC,
            output=str(self.bundle),
        )
        export_bundle(args)

    def test_export_and_verify_round_trip_includes_three_model_families(self) -> None:
        self._export()
        manifest = verify_bundle(self.bundle)
        self.assertEqual(
            set(manifest["models"]),
            {"bge-m3", "reranker", "public-patent-embedding"},
        )
        public = manifest["models"]["public-patent-embedding"]
        self.assertEqual(public["revision"], REV_PUBLIC)
        self.assertEqual(public["source_repo"], "qdrant/bge-large-en-v1.5-onnx")

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

    def test_non_immutable_model_revision_is_rejected(self) -> None:
        args = argparse.Namespace(
            bge_source=str(self.bge), bge_revision="main",
            reranker_source=str(self.reranker), reranker_revision=REV_RERANKER,
            public_patent_source=str(self.public), public_patent_revision=REV_PUBLIC,
            output=str(self.bundle),
        )
        with self.assertRaises(BundleError):
            export_bundle(args)

    def test_symlinks_are_rejected(self) -> None:
        link = self.public / "external-link"
        link.symlink_to(self.bge / "config.json")
        with self.assertRaises(BundleError):
            self._export()

    def test_public_patent_dimension_mismatch_is_rejected(self) -> None:
        (self.public / "config.json").write_text(
            json.dumps({"model_type": "bert", "hidden_size": 768}), encoding="utf-8"
        )
        with self.assertRaises(BundleError):
            self._export()

    def test_install_creates_fastembed_hub_cache_and_refuses_implicit_overwrite(self) -> None:
        self._export()
        target = self.root / "installed"
        install_args = argparse.Namespace(bundle=str(self.bundle), target=str(target), replace=False)
        self.assertEqual(install_command(install_args), 0)
        self.assertTrue((target / "bge-m3" / "model.safetensors").is_file())
        self.assertTrue((target / "reranker" / "tokenizer.json").is_file())
        installed_manifest = json.loads(
            (target / "patentshield-model-bundle-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            installed_manifest["models"]["public-patent-embedding"]["revision"], REV_PUBLIC
        )
        snapshot = (
            target
            / "fastembed-cache"
            / "models--qdrant--bge-large-en-v1.5-onnx"
            / "snapshots"
            / REV_PUBLIC
        )
        self.assertTrue((snapshot / "model.onnx").is_file())
        ref = (
            target
            / "fastembed-cache"
            / "models--qdrant--bge-large-en-v1.5-onnx"
            / "refs"
            / "main"
        )
        self.assertEqual(ref.read_text(encoding="ascii").strip(), REV_PUBLIC)
        with self.assertRaises(BundleError):
            install_command(install_args)

    def test_missing_public_onnx_weights_are_rejected(self) -> None:
        (self.public / "model.onnx").unlink()
        with self.assertRaises(BundleError):
            self._export()

    def test_empty_files_are_rejected(self) -> None:
        (self.public / "model.onnx").write_bytes(b"")
        with self.assertRaises(BundleError):
            self._export()


if __name__ == "__main__":
    unittest.main()
