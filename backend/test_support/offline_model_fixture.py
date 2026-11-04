"""Tiny hash-verified offline model installations for adapter unit tests.

The fixture contains intentionally non-loadable placeholder bytes and must only
be paired with controlled fake model adapters. It exercises the real installed
manifest verifier without downloading or substituting production model weights.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


MODEL_IDS = {
    "bge-m3": "BAAI/bge-m3",
    "reranker": "BAAI/bge-reranker-v2-m3",
    "public-patent-embedding": "BAAI/bge-large-en-v1.5",
}
MODEL_REVISIONS = {
    "bge-m3": "a" * 40,
    "reranker": "b" * 40,
    "public-patent-embedding": "c" * 40,
}


def _write_model_files(directory: Path, files: dict[str, bytes]) -> list[dict[str, Any]]:
    entries = []
    for relative_path, contents in sorted(files.items()):
        path = directory / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        entries.append({
            "path": relative_path,
            "sha256": hashlib.sha256(contents).hexdigest(),
            "size": len(contents),
        })
    return entries


def create_offline_model_installation(root: str | Path) -> dict[str, Any]:
    """Create a schema-v2 installation and matching manifest under ``root``.

    All three supported model records are present because the production
    verifier rejects partial bundles. Revisions are immutable-shaped test IDs;
    the real verifier checks their shape, while the fake adapters avoid loading
    these deliberately synthetic artifacts.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    bge_dir = root / "bge-m3"
    reranker_dir = root / "reranker"
    public_revision = MODEL_REVISIONS["public-patent-embedding"]
    public_cache = root / "fastembed-cache"
    public_snapshot = (
        public_cache
        / "models--qdrant--bge-large-en-v1.5-onnx"
        / "snapshots"
        / public_revision
    )

    model_directories = {
        "bge-m3": bge_dir,
        "reranker": reranker_dir,
        "public-patent-embedding": public_snapshot,
    }
    file_payloads = {
        "bge-m3": {
            "config.json": b'{"fixture":true}\n',
            "tokenizer.json": b'{"fixture":true}\n',
            "model.safetensors": b"test-only synthetic BGE-M3 weights\n",
        },
        "reranker": {
            "config.json": b'{"fixture":true}\n',
            "tokenizer.json": b'{"fixture":true}\n',
            "model.safetensors": b"test-only synthetic reranker weights\n",
        },
        "public-patent-embedding": {
            "config.json": b'{"hidden_size":1024,"fixture":true}\n',
            "tokenizer.json": b'{"fixture":true}\n',
            "tokenizer_config.json": b'{"fixture":true}\n',
            "model.onnx": b"test-only synthetic ONNX weights\n",
        },
    }

    models: dict[str, dict[str, Any]] = {}
    for key, model_dir in model_directories.items():
        files = _write_model_files(model_dir, file_payloads[key])
        models[key] = {
            "model_id": MODEL_IDS[key],
            "revision": MODEL_REVISIONS[key],
            "format": (
                "fastembed-onnx"
                if key == "public-patent-embedding"
                else "transformers"
            ),
            "path": f"models/{key}",
            "files": files,
        }

    # FastEmbed's installed cache layout uses this reference to resolve the
    # snapshot, in addition to the explicit snapshot path verified by the app.
    refs = public_cache / "models--qdrant--bge-large-en-v1.5-onnx" / "refs"
    refs.mkdir(parents=True, exist_ok=True)
    (refs / "main").write_text(public_revision + "\n", encoding="ascii")

    manifest_path = root / "patentshield-model-bundle-manifest.json"
    manifest_path.write_text(
        json.dumps({"schema_version": 2, "models": models}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "manifest_path": str(manifest_path),
        "bge_m3_path": str(bge_dir),
        "reranker_path": str(reranker_dir),
        "public_cache_dir": str(public_cache),
        "public_snapshot_path": str(public_snapshot),
        "revisions": dict(MODEL_REVISIONS),
    }
