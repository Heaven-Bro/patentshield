"""Create, verify, and install offline PatentShield model bundles.

This module deliberately uses only the Python standard library. It never
fetches model artifacts or executes code from model repositories. The bundle
contains the two FlagEmbedding models plus the FastEmbed ONNX model used by
public-patent semantic search.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import secrets
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = 2
REVISION_RE = re.compile(r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
MODEL_SPECS: dict[str, dict[str, Any]] = {
    "bge-m3": {
        "model_id": "BAAI/bge-m3",
        "format": "transformers",
        "required_dimension": 1024,
        "weight_patterns": ("*.safetensors", "pytorch_model*.bin", "tf_model.h5", "model*.onnx"),
        "required_files": ("config.json",),
    },
    "reranker": {
        "model_id": "BAAI/bge-reranker-v2-m3",
        "format": "transformers",
        "required_dimension": None,
        "weight_patterns": ("*.safetensors", "pytorch_model*.bin", "tf_model.h5", "model*.onnx"),
        "required_files": ("config.json",),
    },
    "public-patent-embedding": {
        "model_id": "BAAI/bge-large-en-v1.5",
        "source_repo": "qdrant/bge-large-en-v1.5-onnx",
        "format": "fastembed-onnx",
        "required_dimension": 1024,
        "weight_patterns": ("model.onnx",),
        "required_files": ("config.json", "tokenizer.json", "tokenizer_config.json", "model.onnx"),
    },
}
TRANSFORMER_TOKENIZER_FILES = {
    "tokenizer.json", "tokenizer.model", "sentencepiece.bpe.model", "vocab.txt",
    "spiece.model", "merges.txt", "vocab.json",
}


class BundleError(RuntimeError):
    """Raised when a model bundle is incomplete or does not match its manifest."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_relative(raw: str) -> Path:
    pure = PurePosixPath(raw)
    if (
        not raw
        or pure.is_absolute()
        or ".." in pure.parts
        or "\\" in raw
        or "\x00" in raw
        or pure.as_posix() != raw
        or raw in {".", ".."}
    ):
        raise BundleError(f"Unsafe relative path in model manifest: {raw!r}")
    return Path(*pure.parts)


def _assert_no_symlinks(root: Path, model_key: str) -> list[Path]:
    paths = list(root.rglob("*"))
    for item in paths:
        if item.is_symlink():
            raise BundleError(
                f"{model_key}: symlinks are not allowed in a portable model bundle: {item}"
            )
    return paths


def _validate_model_dir(model_dir: Path, model_key: str) -> None:
    spec = MODEL_SPECS[model_key]
    if not model_dir.is_dir() or model_dir.is_symlink():
        raise BundleError(f"{model_key}: model directory is missing or unsafe: {model_dir}")
    paths = _assert_no_symlinks(model_dir, model_key)
    files = [item for item in paths if item.is_file()]
    names = {item.relative_to(model_dir).as_posix() for item in files}
    missing = sorted(set(spec["required_files"]) - names)
    if missing:
        raise BundleError(f"{model_key}: required file(s) missing: {', '.join(missing)}")
    if not any(any(item.match(pattern) for pattern in spec["weight_patterns"]) for item in files):
        raise BundleError(f"{model_key}: no supported model weight files were found")
    if model_key in {"bge-m3", "reranker"} and not any(
        item.name in TRANSFORMER_TOKENIZER_FILES for item in files
    ):
        raise BundleError(f"{model_key}: tokenizer files were not found")
    for item in files:
        if item.stat().st_size <= 0:
            raise BundleError(f"{model_key}: empty file is not allowed: {item}")

    if model_key == "public-patent-embedding":
        try:
            config = json.loads((model_dir / "config.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BundleError("public-patent-embedding: config.json is invalid") from exc
        dimension = config.get("hidden_size") or config.get("d_model") or config.get("dim")
        if dimension != spec["required_dimension"]:
            raise BundleError(
                "public-patent-embedding: ONNX model config must declare hidden_size/d_model/dim=1024"
            )


def _list_model_files(model_dir: Path) -> list[dict[str, Any]]:
    result = []
    for path in sorted(item for item in model_dir.rglob("*") if item.is_file()):
        rel = path.relative_to(model_dir).as_posix()
        _safe_relative(rel)
        result.append({"path": rel, "sha256": _sha256(path), "size": path.stat().st_size})
    return result


def _validate_revision(revision: str, model_key: str) -> str:
    value = str(revision or "").strip().casefold()
    if not REVISION_RE.fullmatch(value):
        raise BundleError(
            f"{model_key}: revision must be an immutable 40- or 64-character lowercase hex commit ID"
        )
    return value


def export_bundle(args: argparse.Namespace) -> int:
    output = Path(args.output).expanduser().resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise BundleError(f"Output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    models_root = output / "models"
    models_root.mkdir(exist_ok=True)
    source_map = {
        "bge-m3": (Path(args.bge_source).expanduser().resolve(), _validate_revision(args.bge_revision, "bge-m3")),
        "reranker": (Path(args.reranker_source).expanduser().resolve(), _validate_revision(args.reranker_revision, "reranker")),
        "public-patent-embedding": (
            Path(args.public_patent_source).expanduser().resolve(),
            _validate_revision(args.public_patent_revision, "public-patent-embedding"),
        ),
    }
    manifest: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "models": {}}
    for key, (source, revision) in source_map.items():
        _validate_model_dir(source, key)
        target = models_root / key
        shutil.copytree(source, target, symlinks=False)
        _validate_model_dir(target, key)
        model_entry = {
            "model_id": MODEL_SPECS[key]["model_id"],
            "revision": revision,
            "path": f"models/{key}",
            "format": MODEL_SPECS[key]["format"],
            "files": _list_model_files(target),
        }
        if "source_repo" in MODEL_SPECS[key]:
            model_entry["source_repo"] = MODEL_SPECS[key]["source_repo"]
        manifest["models"][key] = model_entry
    manifest_path = output / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    verify_bundle(output)
    print(f"Created and verified offline model bundle: {output}")
    print(f"Manifest SHA-256: {_sha256(manifest_path)}")
    return 0


def verify_bundle(bundle: Path) -> dict[str, Any]:
    bundle = bundle.expanduser().resolve(strict=True)
    manifest_path = bundle / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise BundleError("Bundle must contain a regular manifest.json file")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleError("manifest.json is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise BundleError("Unsupported model bundle manifest schema_version")
    models = manifest.get("models")
    if not isinstance(models, dict) or set(models) != set(MODEL_SPECS):
        raise BundleError("Bundle must include exactly bge-m3, reranker, and public-patent-embedding")
    for key, spec in models.items():
        expected_spec = MODEL_SPECS[key]
        if not isinstance(spec, dict) or spec.get("model_id") != expected_spec["model_id"]:
            raise BundleError(f"Unexpected model_id for {key}")
        if spec.get("format") != expected_spec["format"]:
            raise BundleError(f"Unexpected model format for {key}")
        if "source_repo" in expected_spec and spec.get("source_repo") != expected_spec["source_repo"]:
            raise BundleError(f"Unexpected source_repo for {key}")
        _validate_revision(str(spec.get("revision", "")), key)
        model_rel = _safe_relative(str(spec.get("path", "")))
        model_dir = bundle / model_rel
        if model_dir.is_symlink() or not model_dir.is_dir():
            raise BundleError(f"Model directory is missing or unsafe: {model_rel}")
        if not model_dir.resolve(strict=True).is_relative_to(bundle):
            raise BundleError(f"Model directory escapes bundle root: {model_rel}")
        _validate_model_dir(model_dir, key)
        listed = spec.get("files")
        if not isinstance(listed, list) or not listed:
            raise BundleError(f"Manifest file list is missing for {key}")
        expected: dict[str, dict[str, Any]] = {}
        for item in listed:
            if not isinstance(item, dict) or set(item) != {"path", "sha256", "size"}:
                raise BundleError(f"Malformed file entry in {key}")
            rel = _safe_relative(str(item.get("path", "")))
            sha = item.get("sha256")
            size = item.get("size")
            if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha):
                raise BundleError(f"Malformed SHA-256 entry in {key}: {rel}")
            if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
                raise BundleError(f"Malformed size entry in {key}: {rel}")
            if rel.as_posix() in expected:
                raise BundleError(f"Duplicate manifest file path in {key}: {rel}")
            expected[rel.as_posix()] = item
        actual_paths = {
            item.relative_to(model_dir).as_posix()
            for item in model_dir.rglob("*")
            if item.is_file()
        }
        if actual_paths != set(expected):
            missing = sorted(set(expected) - actual_paths)
            extra = sorted(actual_paths - set(expected))
            raise BundleError(f"File inventory mismatch for {key}; missing={missing}, extra={extra}")
        for rel_text, entry in expected.items():
            file_path = model_dir / _safe_relative(rel_text)
            if file_path.is_symlink() or not file_path.is_file():
                raise BundleError(f"Unsafe or missing model file: {file_path}")
            if file_path.stat().st_size != entry["size"]:
                raise BundleError(f"Size mismatch for model file: {file_path}")
            if _sha256(file_path) != entry["sha256"]:
                raise BundleError(f"SHA-256 mismatch for model file: {file_path}")
    return manifest


def verify_command(args: argparse.Namespace) -> int:
    manifest = verify_bundle(Path(args.bundle))
    print("Offline model bundle verified.")
    for key, spec in sorted(manifest["models"].items()):
        total = sum(int(item["size"]) for item in spec["files"])
        print(f"  {key}: {len(spec['files'])} files, {total} bytes, revision={spec['revision']}")
    return 0


def _copy_verified_tree(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, symlinks=False)
    if _list_model_files(destination) != _list_model_files(source):
        raise BundleError(f"Copied model changed during installation: {source.name}")


def install_command(args: argparse.Namespace) -> int:
    bundle = Path(args.bundle).expanduser().resolve(strict=True)
    manifest = verify_bundle(bundle)
    target_root = Path(args.target).expanduser().resolve()
    target_root.mkdir(parents=True, exist_ok=True)

    public_spec = manifest["models"]["public-patent-embedding"]
    public_revision = public_spec["revision"]
    fastembed_rel = Path("models--qdrant--bge-large-en-v1.5-onnx")
    install_targets = {
        "bge-m3": target_root / "bge-m3",
        "reranker": target_root / "reranker",
        "fastembed-cache": target_root / "fastembed-cache",
    }
    metadata_path = target_root / "patentshield-model-bundle-manifest.json"
    existing = [path for path in install_targets.values() if path.exists() or path.is_symlink()]
    if metadata_path.exists() or metadata_path.is_symlink():
        existing.append(metadata_path)
    if existing and not args.replace:
        raise BundleError(
            "Target already contains model directories; use --replace only after a verified backup: "
            + ", ".join(str(path) for path in existing)
        )

    staging = Path(tempfile.mkdtemp(prefix=".patentshield-model-stage-", dir=target_root))
    backups: list[tuple[Path, Path]] = []
    published: list[Path] = []
    metadata_published = False
    try:
        _copy_verified_tree(bundle / _safe_relative(manifest["models"]["bge-m3"]["path"]), staging / "bge-m3")
        _copy_verified_tree(bundle / _safe_relative(manifest["models"]["reranker"]["path"]), staging / "reranker")

        fastembed_root = staging / "fastembed-cache"
        snapshot = fastembed_root / fastembed_rel / "snapshots" / public_revision
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        _copy_verified_tree(
            bundle / _safe_relative(public_spec["path"]), snapshot
        )
        refs = fastembed_root / fastembed_rel / "refs"
        refs.mkdir(parents=True, exist_ok=True)
        (refs / "main").write_text(public_revision + "\n", encoding="ascii")

        # Verify the exact Hugging Face cache layout consumed by FastEmbed 0.5.x.
        if (refs / "main").read_text(encoding="ascii").strip() != public_revision:
            raise BundleError("FastEmbed local cache reference was not staged correctly")
        _validate_model_dir(snapshot, "public-patent-embedding")

        for key, destination in install_targets.items():
            staged = staging / key
            if destination.exists() or destination.is_symlink():
                backup = target_root / f".{key}.backup-{int(time.time())}-{secrets.token_hex(4)}"
                os.replace(destination, backup)
                backups.append((destination, backup))
            os.replace(staged, destination)
            published.append(destination)

        staged_metadata = staging / "patentshield-model-bundle-manifest.json"
        shutil.copy2(bundle / "manifest.json", staged_metadata)
        if metadata_path.exists() or metadata_path.is_symlink():
            backup = target_root / f".model-manifest.backup-{int(time.time())}-{secrets.token_hex(4)}"
            os.replace(metadata_path, backup)
            backups.append((metadata_path, backup))
        os.replace(staged_metadata, metadata_path)
        metadata_published = True

        print(f"Installed verified offline models into {target_root}")
        print(f"  Bundle metadata: {metadata_path}")
        print(f"  BGE-M3: {install_targets['bge-m3']}")
        print(f"  Reranker: {install_targets['reranker']}")
        print(f"  FastEmbed cache: {install_targets['fastembed-cache']}")
        if backups:
            print("Previous model directories retained as hidden backups for rollback.")
        return 0
    except Exception:
        if metadata_published and (metadata_path.exists() or metadata_path.is_symlink()):
            if metadata_path.is_dir() and not metadata_path.is_symlink():
                shutil.rmtree(metadata_path)
            else:
                metadata_path.unlink()
        for destination in reversed(published):
            if destination.is_dir() and not destination.is_symlink():
                shutil.rmtree(destination)
            elif destination.exists() or destination.is_symlink():
                destination.unlink()
        for original, backup in reversed(backups):
            if backup.exists() or backup.is_symlink():
                os.replace(backup, original)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    exporter = commands.add_parser("export", help="materialize models and produce a hash manifest")
    exporter.add_argument("--bge-source", required=True)
    exporter.add_argument("--bge-revision", required=True)
    exporter.add_argument("--reranker-source", required=True)
    exporter.add_argument("--reranker-revision", required=True)
    exporter.add_argument("--public-patent-source", required=True)
    exporter.add_argument("--public-patent-revision", required=True)
    exporter.add_argument("--output", required=True)
    exporter.set_defaults(handler=export_bundle)
    verifier = commands.add_parser("verify", help="verify manifest inventory and hashes")
    verifier.add_argument("--bundle", required=True)
    verifier.set_defaults(handler=verify_command)
    installer = commands.add_parser("install", help="install verified model files without network access")
    installer.add_argument("--bundle", required=True)
    installer.add_argument("--target", required=True)
    installer.add_argument("--replace", action="store_true", help="retain backups and replace existing model directories")
    installer.set_defaults(handler=install_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (BundleError, OSError, ValueError) as exc:
        print(f"model-bundle error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
